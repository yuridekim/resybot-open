# ResyGrabber 사용 가이드 (나를 위한 메모)

Resy 인기 식당 예약을 오픈 시각에 맞춰 자동으로 잡는 봇.
흐름: 오픈 직전 시작 → 0.05~0.3초 간격으로 빈자리 조회 → 원하는 날짜/시간 슬롯이 뜨면 즉시 예약 → 실패하면 다음 슬롯 → 4초 후 종료.

---

## 0. 처음 한 번만: 설치

```bash
git clone https://github.com/yuridekim/resybot-open.git ~/dev/resybot-open
cd ~/dev/resybot-open
python3.12 -m venv server/venv
source server/venv/bin/activate
pip install -r server/requirements.txt
grep -v -E "^(uuid|pyinstaller)" client/requirements.txt > /tmp/client-req.txt
pip install -r /tmp/client-req.txt
```

> ⚠️ 프로젝트를 `~/Documents`, `~/Desktop`(iCloud 동기화 폴더)에 두지 말 것.
> 파일이 클라우드로 빠져서 `pip install`이 끝없이 멈춤.

---

## 1. 필요한 정보 3가지

먼저 터미널에 토큰을 넣어둔다 (화면에 안 보임, 이 창에서만 유효):

```bash
read -s "TOKEN?Auth Token: "; echo
H=(-H 'Authorization: ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"'
   -H "X-Resy-Auth-Token: $TOKEN" -H "X-Resy-Universal-Auth: $TOKEN"
   -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
   -H 'Accept: application/json, text/plain, */*'
   -H 'Origin: https://resy.com' -H 'X-Origin: https://resy.com' -H 'Referer: https://resy.com/')
```

### ① Auth Token
1. Chrome으로 resy.com **로그인**
2. `Cmd+Option+I` → **Network** 탭 → `Cmd+R`
3. Filter에 `api.resy` → 아무 요청 클릭 (예: `upcoming`, `user`)
4. **Headers → Request Headers**의 `x-resy-auth-token` 값 (`eyJ...`) 복사

- `x-resy-auth-token`이 없으면 로그인이 풀린 것
- 토큰은 몇 달 뒤 만료됨 → 쓸 때마다 새로 받기
- **절대 아무 데도 붙여넣지 말 것** (계정 비밀번호와 같음)

### ② Payment ID
```bash
curl -s "${H[@]}" https://api.resy.com/2/user | python3 -c "import sys,json; [print(p['id'], p.get('type'), p.get('display')) for p in json.load(sys.stdin).get('payment_methods',[])]"
```
→ 쓸 카드의 숫자 ID

### ③ Venue ID (식당 ID)
식당 Resy URL의 이름 부분을 넣는다. 예: `resy.com/cities/new-york-ny/venues/monkey-bar-nyc` → `monkey-bar-nyc`
```bash
SLUG=monkey-bar-nyc
curl -s "${H[@]}" "https://api.resy.com/3/venue?url_slug=$SLUG&location=new-york-ny" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['id']['resy'], d['name'])"
```
ID로 식당 이름 확인: `curl -s "${H[@]}" "https://api.resy.com/3/venue?id=3631" | python3 -c "import sys,json; print(json.load(sys.stdin)['name'])"`

> 메모: Monkey Bar = `60058`, Hi-Collar = `3631`

---

## 2. 사전 확인 (예약 전날 밤)

**인원수가 되는지 + 어떤 날짜가 열려 있는지** (마지막 날짜 = 현재 오픈된 끝 날짜):
```bash
V=60058; N=7
curl -s "${H[@]}" "https://api.resy.com/4/venue/calendar?venue_id=$V&num_seats=$N&start_date=$(date +%F)&end_date=$(date -v+60d +%F)" | python3 -c "import sys,json; [print(e['date'], e['inventory']['reservation']) for e in json.load(sys.stdin)['scheduled']]"
```
- 결과의 마지막 날짜 + 1일 = 다음 오픈 날짜
- 식당의 오픈 규칙(예: "매일 9시, 20일 뒤 날짜") 확인
- 단체는 온라인 예약이 안 되는 식당도 있음

---

## 3. 봇 설정 & 실행

```bash
sudo sntp -sS time.apple.com        # 맥 시계 맞추기 (초 단위 정확도 중요)
cd ~/dev/resybot-open
source server/venv/bin/activate
caffeinate -i python start.py       # 서버 + 메뉴 동시 실행, 맥 잠들기 방지
```

| 메뉴 | 할 일 |
|---|---|
| **4) Manage Accounts** | 토큰 + Payment ID 추가 (만료된 예전 계정은 삭제) |
| **6) View Reservations** | 에러 없으면 토큰 정상 |
| **1) Show tasks → Add task** | 아래 표대로 입력. 테스트 태스크는 지우기 |
| **8) Schedule Tasks** | 오픈 1초 전 (예: `08:59:59`), `Once` → 확인 메시지에 초까지 나오는지 확인 |
| 7) Start Tasks | 저장된 **모든** 태스크 즉시 실행 (테스트용) |

**태스크 입력값**

| 항목 | 예시 | 비고 |
|---|---|---|
| restaurant ID | `60058` | venue id |
| party sizes | `7` | `2,3`처럼 여러 개 가능 |
| start / end date | `2026-11-12` | 하루만이면 둘 다 같게 |
| start / end time | `17:30` / `21:00` | 이 범위의 슬롯만 (양 끝 포함) |
| delay | `300` | ms. 그대로 두기 |

- 스케줄은 메모리에만 저장 → **프로그램 끄면 사라짐**. 계정/태스크는 json에 저장돼서 남음
- 실행 중엔 터미널 닫지 말고, 노트북 덮개 열어두기
- 오픈 시각에 **휴대폰으로도 직접 시도** (같은 계정이어도 OK)

---

## 4. 로그 읽기

```
[08:59:59.0xx] Task started: ...                      시작
[..] Check #1 2026-11-12: 0 slots open, 0 in your ...   조회 중 (아직 안 열림)
[..] Trying 2026-11-12 18:30:00 ...                   예약 시도
[..] Booking ... failed: ...                          그 슬롯 실패 → 바로 다음 슬롯
[..] BOOKED ...                                       ✅ 성공
[..] Gave up ... after 4s                             ❌ 시간 초과
[..] Auth token rejected (401)                        토큰 만료 → 새로 받기
[..] Local server not reachable                       서버 안 켜짐 → start.py로 실행
```
결과는 Resy 앱/이메일로도 꼭 확인.

---

## 5. 조정하고 싶을 때 (`client/task_executor.py` 맨 위)

| 값 | 현재 | 의미 |
|---|---|---|
| `MAX_RUNTIME_SECONDS` | `4` | 시작 후 몇 초 뒤 포기 (08:59:59 + 4 = 09:00:03) |
| `BURST_DELAY_SECONDS` | `0.05` | 매 분 :00.0~:01.5초 동안의 조회 간격 |
| `MAX_BACKOFF_SECONDS` | `1` | Resy 에러 시 최대 대기 |

요청량: 4초에 약 11번 (페이지 한 번 여는 것보다 적음). 차단 위험 낮게 유지하려면 늘리지 말 것.

---

## 6. 주의 / 트러블슈팅

- **테스트 = 진짜 예약.** 자리 있는 날짜로 돌리면 실제로 잡힘 → 취소 정책 먼저 확인, 바로 취소
- **Resy API가 바뀌면** 500/에러가 남. 브라우저 Network 탭의 요청 헤더와 비교해서 맞추기
  (2026-10: `Origin`, `X-Origin` 헤더가 필수가 돼서 추가함)
- Resy 이용약관상 봇은 금지 → 가끔 본인 예약용으로만
- 원본 레포: `korbinschulz/resybot-open` (`upstream` 리모트)
