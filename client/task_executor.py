import random
import time
import datetime
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import capsolver
from urllib.parse import quote

# How long a task keeps trying before giving up (seconds).
# Start at 08:59:59 + 4s = stops at ~09:00:03.
MAX_RUNTIME_SECONDS = 4
# Longest wait between retries after Resy errors / rate limits (seconds).
MAX_BACKOFF_SECONDS = 1
# Burst mode: during the first 1.5s of the minute (xx:00:00.0 - xx:00:01.5), when seats drop,
# wait only this long between checks (~3-5 checks per second, limited by Resy's response time).
BURST_DELAY_SECONDS = 0.05


def in_burst_window():
    now = datetime.datetime.now()
    return now.second == 0 or (now.second == 1 and now.microsecond < 500000)


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def format_proxy(proxy_str):
    parts = proxy_str.split(':')
    if len(parts) == 4:
        ip, port, user, password = parts
        url = f'http://{user}:{password}@{ip}:{port}'
    else:
        url = f'http://{proxy_str}'
    return {'http': url, 'https': url}


def date_range(start_date, end_date):
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    days = []
    while start <= end:
        days.append(start.isoformat())
        start += datetime.timedelta(days=1)
    return days


def slot_minutes(slot):
    # Minutes since midnight. Prefer the explicit start time ("2026-11-12 19:30:00"); fall back to the config token.
    try:
        hh, mm = slot['date']['start'].split(' ')[1].split(':')[:2]
    except Exception:
        hh, mm = slot['config']['token'].split('/')[8].split(':')[:2]
    return int(hh) * 60 + int(mm)


def slot_date(slot):
    # "2026-11-12". Resy's /4/find can return slots for a DIFFERENT day than requested, so always check.
    try:
        return slot['date']['start'].split(' ')[0]
    except Exception:
        m = re.search(r'\d{4}-\d{2}-\d{2}', slot.get('config', {}).get('token', ''))
        return m.group(0) if m else None


def to_minutes(value, is_end):
    # "17:30" -> 1050. A bare hour like 21 keeps the old meaning: start = 21:00, end = 21:59.
    text = str(value).strip()
    if ':' in text:
        hh, mm = text.split(':')[:2]
        return int(hh) * 60 + int(mm)
    return int(text) * 60 + (59 if is_end else 0)


def fmt_minutes(m):
    return f'{m // 60:02d}:{m % 60:02d}'


def execute_task(task, capsolver_key, capmonster_key, proxies, webhook_url):
    auth_token = task['auth_token']
    payment_id = task['payment_id']
    restaurant_id = task['restaurant_id']
    party_sz = task['party_sz']
    start_min = to_minutes(task['start_time'], is_end=False)
    end_min = to_minutes(task['end_time'], is_end=True)
    delay = max(int(task['delay']), 100) / 1000
    days = date_range(task['start_date'], task['end_date'])

    headers = {
        'X-Resy-Auth-Token': auth_token,
        'Authorization': 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'X-Resy-Universal-Auth': auth_token,
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://resy.com/',
        'Origin': 'https://resy.com',
        'X-Origin': 'https://resy.com',
    }

    deadline = time.time() + MAX_RUNTIME_SECONDS
    backoff = delay
    tried = set()   # slots that already failed, so we move on to the next one
    attempts = 0

    log(f'Task started: venue {restaurant_id}, {party_sz} people, {days[0]}~{days[-1]}, {fmt_minutes(start_min)}-{fmt_minutes(end_min)}, every {delay}s')

    while time.time() < deadline:
        attempts += 1
        select_proxy = format_proxy(random.choice(proxies)) if proxies else {}

        for day in days:
            try:
                url = f"https://api.resy.com/4/find?lat=0&long=0&day={day}&party_size={party_sz}&venue_id={restaurant_id}"
                response = requests.get(url, headers=headers, proxies=select_proxy, timeout=10)
            except Exception as e:
                log(f'Request error ({e}); retrying')
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            if response.status_code == 401:
                send_discord_notification(webhook_url, f'Auth token rejected (401). Get a new token. Stopping task for {restaurant_id}.')
                return
            if response.status_code != 200:
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                log(f'HTTP {response.status_code} from Resy; slowing down to {backoff:.1f}s')
                continue
            backoff = delay

            try:
                venues = response.json().get('results', {}).get('venues', [])
            except Exception:
                log('Could not read Resy response; retrying')
                continue
            slots = venues[0].get('slots', []) if venues else []

            candidates = []
            for slot in slots:
                try:
                    minutes = slot_minutes(slot)
                except Exception:
                    continue
                token = slot['config']['token']
                if slot_date(slot) != day:
                    continue  # wrong date - never book it
                if start_min <= minutes <= end_min and token not in tried:
                    candidates.append(slot)

            if attempts == 1 or attempts % 5 == 0 or candidates:
                log(f'Check #{attempts} {day}: {len(slots)} slots open, {len(candidates)} in your time range')

            for slot in candidates:
                config_token = slot['config']['token']
                tried.add(config_token)
                label = slot.get('date', {}).get('start', config_token)
                log(f'Trying {label} ...')

                book_token = get_details(day, party_sz, config_token, restaurant_id, headers, select_proxy)
                if not book_token:
                    log(f'Could not get booking token for {label}; trying next slot')
                    continue

                result = book_reservation(book_token, auth_token, payment_id, day, party_sz, restaurant_id, config_token, headers, select_proxy)
                if 'reservation_id' in result or ('specs' in result and 'reservation_id' in result['specs']):
                    send_discord_notification(webhook_url, f'BOOKED {label} at venue {restaurant_id} for {party_sz} - {result}')
                    return
                log(f'Booking {label} failed: {str(result)[:300]}')

        # Burst only when Resy is healthy; after an error, respect the backoff.
        if backoff == delay and in_burst_window():
            time.sleep(BURST_DELAY_SECONDS)
        else:
            time.sleep(backoff)

    send_discord_notification(webhook_url, f'Gave up on venue {restaurant_id} after {MAX_RUNTIME_SECONDS}s ({attempts} checks).')


def get_captcha_token(captcha_key, site_key, url, proxy):
    solution = capsolver.solve({
        "type": "RecaptchaV2Task",
        "websiteKey": site_key,
        "websiteURL": url,
        "proxy": proxy['http']
    })
    gRecaptchaResponse = solution['gRecaptchaResponse']
    return gRecaptchaResponse


def get_details(day, party_size, config_token, restaurant_id, headers, select_proxy):
    url = 'http://127.0.0.1:8000/api/get-details'
    payload = {
        'day': day,
        'party_size': party_size,
        'config_token': config_token,
        'restaurant_id': restaurant_id,
        'headers': headers,
        'select_proxy': select_proxy
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        log(f'Local server not reachable ({e}). Is the server running?')
        return None

    if response.status_code != 200:
        log(f'Failed to get details - {response.text[:200]} - {response.status_code}')
        return None

    return response.json().get('response_value')


def book_reservation(book_token, auth_token, payment_id, day, party_size, restaurant_id, config_token, headers, select_proxy):
    url = 'http://127.0.0.1:8000/api/book-reservation'
    payload = {
        'book_token': book_token,
        'auth_token': auth_token,
        'payment_id': payment_id,
        'day': day,
        'party_size': party_size,
        'restaurant_id': restaurant_id,
        'config_token': config_token,
        'headers': headers,
        'select_proxy': select_proxy
    }
    try:
        response = requests.post(url, json=payload, timeout=20)
        return response.json()
    except Exception as e:
        return {'error': str(e)}


def send_discord_notification(webhook_url, message):
    if not webhook_url:
        log(f"[Notification]: {message}")
        return
    try:
        data = {"content": message}
        requests.post(webhook_url, json=data, timeout=10)
    except Exception as e:
        log(f"[Notification Error]: Failed to send to Discord: {e}")
        log(f"[Notification Content]: {message}")


def run_tasks_concurrently(tasks, capsolver_key, capmonster_key, proxies, webhook_url):
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(execute_task, task, capsolver_key, capmonster_key, proxies, webhook_url) for task in tasks]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print('Failed to execute task')
                print(e)
