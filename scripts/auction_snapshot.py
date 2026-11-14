"""포에버 경매장 시세 정리 (GitHub 자동 작업이 1시간마다 실행 — 개발자 PC에서도 시험 가능).

  python tools/auction_snapshot.py [출력 폴더=dist/auction] [지역=kr]

열쇠: 환경 변수 BNET_CLIENT_ID / BNET_CLIENT_SECRET (GitHub Secrets), 없으면 app/config/battlenet_client.json.
표준 라이브러리만 씀.

출력 (<출력>/<지역>/):
  index.json            경매장 목록·갱신 시각·매물 수
  house-<id>.json       아이템별 [id, 최저 개당가(동), 총 수량, 매물 수, 중간 개당가, [[가격, 수량], … 싼 순 최대 8단]]
                        (0.7.x 앱용 — 무작위 효과를 한 줄로 합침. 그 앱들이 다 바뀔 때까지 같이 만든다)
  house2-<id>.json      같은 모양인데 첫 칸이 "아이템id" 또는 "아이템id:효과번호" — 무작위 효과(○○의)마다 따로 (0.8 앱부터)
  items.json            아이템 정보 {id: [이름, 품질, 분류, 아이콘 번호,   ← 0.7.x 앱은 여기까지만 읽음
                                       분류id, 세부id, 세부 이름, 부위, 아이템 레벨, 착용 레벨, 툴팁, 상점 판매가]}
                        툴팁 = [[능력치 앞 줄…], [능력치 줄…], [뒤 줄…]], 줄 = [글, 색] (""=흰, "g"=초록 효과, "d"=노랑, "r"=왼쪽\t오른쪽)
                        한 번 찾은 것은 다음에 다시 씀(캐시)
  hist-<id>.json.gz     시세 기록 원본 (다음 실행이 이어 씀, 앱은 안 받음 — 시안 v15, DECISIONS 2026-11-14)
                        {"v":1, "start": 첫 기록 시각, "h": {열쇠: [[시각, 최저, 중간, 수량], … 최근 7일 1시간 단위]},
                                                    "d": {열쇠: [[날, 최저, 중간, 수량], … 최근 90일 하루 단위]}}
                        시각 = 1970년부터 센 시간(UTC), 날 = 한국 시각 날짜 번호. 하루 = 그날 최저가의 최저, 중간가·수량은 평균
  trend-<id>.json.gz    앱이 받는 요약 {"updated", "now": 지금 시각, "start", "k": {열쇠: "<JSON 글>"}}
                        글 = {"h": 48시간(1시간), "s": 7일(6시간), "d": 90일(하루)} — 각각 pack() 모양
                        [가장 오래된 칸(몇 칸 전), [최저…], [중간…], [수량…]] 칸마다 하나(빈 칸 null), 값은 앞 값과의 차이
                        (열쇠마다 글로 두어 앱이 고른 아이템 하나만 풀어 읽음)
  suffix.json           무작위 효과 {"아이템id:효과번호": [효과 이름("고래의"), [[능력치·효과 줄, 색], …]]}
                        블리자드는 효과 번호만 주므로 와우헤드 툴팁에서 찾음 (DECISIONS 2026-11-14). 캐시

경매장 묶음 = 서버군 (2026-11-13 확인): 경매장 id 110~125 는 「서버군마다 얼라이언스·호드·검은바다」.
  묶음 1(110·113·116)=PvP, 2(111·114·117)=일반, 3(112·115·118)=항상 빔, 4(119~121)=롤플레잉, 5(123~125)=롤플레잉 PvP
  (미국·유럽 서버군 4개 → 1·2·4·5 묶음에 매물, 한국·대만 2개 → 1·2. 블리자드 자료에 서버군 이름이 직접 붙지 않아 순서로 맞춤)
"""
from __future__ import annotations

import base64
import gzip
import html
import json
import re
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALE = {"kr": "ko_KR", "tw": "zh_TW", "eu": "en_GB", "us": "en_US"}
# 아이템 이름: 포에버 자료에 빠진 것이 많아 클래식 → 본섭 순서로 찾음 (2026-11-13 시험)
ITEM_NS = ("static-forever-{r}", "static-classic1x-{r}", "static-classic-{r}", "static-{r}")
HOUSE_GROUP = {110: 1, 113: 1, 116: 1, 111: 2, 114: 2, 117: 2, 112: 3, 115: 3, 118: 3,
               119: 4, 120: 4, 121: 4, 123: 5, 124: 5, 125: 5}
GROUP_RULESET = {1: 1, 2: 2, 4: 3, 5: 15}           # 묶음 → 서버군 id
FACTION = {"얼라이언스": "alliance", "호드": "horde", "검은바다": "neutral"}
LADDER = 8
QUALITY = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4, "LEGENDARY": 5, "ARTIFACT": 6, "HEIRLOOM": 7}
ITEM_FIELDS = 12                                   # items.json 한 아이템의 칸 수 (이보다 짧으면 예전 캐시 → 다시 찾음)
# 블리자드 한국어 이름이 한손·양손 모두 「도끼」라 나눔 (무기 세부 id)
WEAPON_SUB = {0: "한손 도끼", 1: "양손 도끼", 4: "한손 둔기", 5: "양손 둔기", 7: "한손 도검", 8: "양손 도검"}
WOWHEAD_TIP = "https://nether.wowhead.com/forever/ko/tooltip/item/{i}?bonus={b}"
KST = 9                                            # 하루·6시간 묶음은 한국 시각 기준
HIST_HOURS, HIST_DAYS, TREND_HOURS = 7 * 24, 90, 48


def http(url, data=None, headers=None, tries=3):
    for k in range(tries):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "OWWarroom-auction", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and k + 1 < tries:
                time.sleep(2 * (k + 1))
                continue
            return e.code, e.read()
        except (urllib.error.URLError, OSError):
            if k + 1 < tries:
                time.sleep(2 * (k + 1))
                continue
            raise


def creds():
    cid, sec = os.environ.get("BNET_CLIENT_ID"), os.environ.get("BNET_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    d = json.loads((ROOT / "app" / "config" / "battlenet_client.json").read_text(encoding="utf-8-sig"))
    return d["client_id"], d["client_secret"]


class Api:
    def __init__(self, region):
        self.region, self.host = region, f"https://{region}.api.blizzard.com"
        cid, sec = creds()
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        st, b = http("https://oauth.battle.net/token", b"grant_type=client_credentials",
                     {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        if st != 200:
            raise SystemExit(f"토큰 실패 ({st})")
        self.tok = json.loads(b)["access_token"]
        self.calls = 0

    def get(self, path, ns):
        q = urllib.parse.urlencode({"namespace": ns.format(r=self.region), "locale": LOCALE[self.region]})
        self.calls += 1
        st, b = http(f"{self.host}{path}?{q}", headers={"Authorization": f"Bearer {self.tok}"})
        return st, (json.loads(b) if st == 200 and b else None)


def variant(item: dict) -> str:
    """경매 아이템 → "id" 또는 "id:효과번호" (무작위 효과 = bonus_lists, 2026-11-14 확인: 장비 매물의 18%)."""
    b = item.get("bonus_lists") or []
    return f"{item['id']}:{'-'.join(map(str, b))}" if b else str(item["id"])


def summarize(auctions: list, commodities: list, split: bool = False) -> list:
    """아이템별 [id, 최저 개당가, 총 수량, 매물 수, 중간 개당가]. 장비는 buyout/수량, 재료는 unit_price.
    split=True 면 무작위 효과마다 따로 (첫 칸이 "id" / "id:효과" 문자열)."""
    per: dict = {}
    key = variant if split else (lambda it: it["id"])
    for a in auctions:
        q = a.get("quantity") or 1
        price = a.get("buyout") or a.get("unit_price")
        if not price:
            continue                                  # 즉시 구매가 없는 입찰 전용
        per.setdefault(key(a["item"]), []).append((price // q if a.get("buyout") else price, q))
    for a in commodities:
        if a.get("unit_price"):
            per.setdefault(key(a["item"]), []).append((a["unit_price"], a.get("quantity") or 1))
    out = []
    for iid, rows in per.items():
        prices = sorted(p for p, _ in rows)
        ladder: dict[int, int] = {}
        for pr, q in sorted(rows):
            if pr in ladder or len(ladder) < LADDER:
                ladder[pr] = ladder.get(pr, 0) + q
        out.append([iid, prices[0], sum(q for _, q in rows), len(rows), int(statistics.median(prices)),
                    [[pr, q] for pr, q in ladder.items()]])
    out.sort(key=lambda r: -r[3])
    return out


def _ds(v) -> str:
    """블리자드 표시 글: {"display_string": …} / {"display": {"display_string": …}} / 글."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("display_string") or (v.get("display") or {}).get("display_string") or ""
    return ""


def tooltip(js: dict) -> list:
    """아이템 API 응답 → 게임 툴팁 줄 [[앞 줄…], [능력치…], [뒤 줄…]] (줄 = [글, 색]).
    무작위 효과 장비는 화면에서 가운데(능력치)를 그 효과의 능력치로 바꿔 보여 준다."""
    p = js.get("preview_item") or {}
    head, stats, tail = [], [], []
    for k in ("name_description", "binding", "unique_equipped"):
        if p.get(k):
            head.append([_ds(p[k]) or (p[k].get("name") if isinstance(p[k], dict) else ""), ""])
    if p.get("item_starts_quest"):
        head.append([_ds(p["item_starts_quest"]) or "퀘스트 시작 아이템", ""])
    if js.get("is_equippable") and (js.get("inventory_type") or {}).get("type") not in (None, "NON_EQUIP", "BAG"):
        sub = "" if p.get("is_subclass_hidden") else (p.get("item_subclass") or {}).get("name", "")
        head.append([f"{(js.get('inventory_type') or {}).get('name', '')}\t{sub}", "r"])
    w = p.get("weapon")
    if w:
        head.append([f"{_ds(w.get('damage'))}\t{_ds(w.get('attack_speed'))}", "r"])
        if _ds(w.get("dps")):
            head.append([_ds(w["dps"]), ""])
    for k in ("armor", "shield_block", "container_slots", "charges"):
        if p.get(k) and _ds(p[k]):
            head.append([_ds(p[k]), ""])
    for s in p.get("stats") or []:
        if _ds(s):
            stats.append([_ds(s), ""])
    for s in p.get("spells") or []:
        if s.get("description"):
            stats.append([s["description"], "g"])
    if p.get("durability"):
        tail.append([_ds(p["durability"]), ""])
    req = p.get("requirements") or {}
    for k in ("playable_classes", "level", "skill", "reputation"):
        if req.get(k) and _ds(req[k]):
            tail.append([_ds(req[k]), ""])
    if p.get("set") and _ds(p["set"]):
        tail.append([_ds(p["set"]), "d"])
        for e in p["set"].get("effects") or []:
            tail.append([_ds(e), "x"])                       # 세트 효과 (회색)
    if p.get("description"):
        tail.append([f"\"{p['description']}\"", "d"])
    return [head, stats, tail]


def item_entry(js: dict, icon: str) -> list:
    cls, sub = js.get("item_class") or {}, js.get("item_subclass") or {}
    sub_name = WEAPON_SUB.get(sub.get("id"), sub.get("name", "")) if cls.get("id") == 2 else sub.get("name", "")
    slot = (js.get("inventory_type") or {})
    sell = js.get("sell_price") if isinstance(js.get("sell_price"), int) else 0
    return [js["name"], QUALITY.get((js.get("quality") or {}).get("type"), 1), cls.get("name", ""), icon,
            cls.get("id", -1), sub.get("id", -1), sub_name,
            slot.get("name", "") if slot.get("type") not in (None, "NON_EQUIP") else "",
            js.get("level") or 0, js.get("required_level") or 0, tooltip(js), sell]


def lookup_names(api: Api, ids: list, cache: dict, budget: int = 9000) -> int:
    # 예전 캐시(칸이 모자람)도 다시 — 아이콘은 있으면 그대로 씀
    todo = [i for i in ids if str(i) not in cache or len(cache[str(i)]) < ITEM_FIELDS][:budget]

    def icon(i):
        st, js = api.get(f"/data/wow/media/item/{i}", "static-forever-{r}")
        for a in (js or {}).get("assets", []):
            if a.get("key") == "icon":
                return a.get("value", "").rsplit("/", 1)[-1].split(".")[0]      # forever-kr/icons/56/<번호>.jpg
        return ""

    def one(i):
        old = cache.get(str(i)) or []
        for ns in ITEM_NS:
            st, js = api.get(f"/data/wow/item/{i}", ns)
            if st == 200 and js and js.get("name"):
                return i, item_entry(js, old[3] if len(old) > 3 and old[3] else icon(i))
            time.sleep(0.02)
        return i, None
    with ThreadPoolExecutor(max_workers=16) as ex:          # 블리자드 한도 초당 100회 아래로
        for i, v in ex.map(one, todo):
            cache[str(i)] = v or ["", 1, "", "", -1, -1, "", "", 0, 0, [[], [], []], 0]
    return len(todo)


def parse_wowhead(js: dict) -> list | None:
    """와우헤드 툴팁(무작위 효과 포함) → [효과 이름, [[능력치·효과 줄, 색], …]]."""
    t = js.get("tooltip") or ""
    m = re.search(r'<b class="q\d">(.*?)</b>', t)
    if not m:
        return None
    full, base = html.unescape(m.group(1)).strip(), (js.get("name") or "").strip()
    suffix = full[len(base):].strip() if base and full.startswith(base) else ""
    lines = []
    for m in re.finditer(r'<span><!--stat\d+-->([^<]+)</span>|<span class="q2">((?:착용|사용|발동) 효과:.*?)</span>(?=<!--|<br>|</td>)', t):
        s = m.group(1) or re.sub(r"<[^>]+>", "", m.group(2))
        lines.append([html.unescape(s).strip(), "" if m.group(1) else "g"])
    return [suffix, lines]


def lookup_suffixes(keys: list, cache: dict, budget: int = 9000, fetch=None) -> int:
    """무작위 효과 "id:효과" → 와우헤드 툴팁. 못 받은 것은 다음 실행 때 다시 (404 등 없는 것은 빈 값으로 기억)."""
    todo = [k for k in keys if ":" in k and k not in cache][:budget]

    def get(url):
        if fetch:
            return fetch(url)
        st, b = http(url, headers={"Accept": "application/json"})
        return st, (json.loads(b) if st == 200 and b else None)

    def one(k):
        i, b = k.split(":", 1)
        try:
            st, js = get(WOWHEAD_TIP.format(i=i, b=b.replace("-", ":")))
        except (urllib.error.URLError, OSError, ValueError):
            return k, None
        if st == 404:
            return k, ["", []]
        return k, parse_wowhead(js) if st == 200 and js else None
    n = 0
    with ThreadPoolExecutor(max_workers=6) as ex:           # 와우헤드에 부담 주지 않게 천천히
        for k, v in ex.map(one, todo):
            if v is not None:
                cache[k] = v
                n += 1
    return n


# ---------- 시세 기록 (시안 v15) ----------
def day_of(hr: int) -> int:
    return (hr + KST) // 24


def six_of(hr: int) -> int:
    return (hr + KST) // 6


def _agg(pts: list) -> list:
    """[[_, 최저, 중간, 수량]…] → [최저의 최저, 중간 평균, 수량 평균]"""
    return [min(p[1] for p in pts), round(sum(p[2] for p in pts) / len(pts)), round(sum(p[3] for p in pts) / len(pts))]


def update_history(hist: dict, rows: list, hr: int) -> dict:
    """한 시간치 시세(house2 줄)를 기록에 덧붙임. 같은 시각을 또 넣으면(다시 실행) 바꿔 씀."""
    hist.setdefault("v", 1)
    hist.setdefault("start", hr)
    H, D = hist.setdefault("h", {}), hist.setdefault("d", {})
    for key, mn, qty, _n, med, *_ in rows:
        pts = H.setdefault(str(key), [])
        if pts and pts[-1][0] == hr:
            pts.pop()
        pts.append([hr, mn, med, qty])
    cut_h, today = hr - HIST_HOURS + 1, day_of(hr)
    first_full = day_of(cut_h) + 1                     # 1시간 기록이 하루를 다 덮는 첫날부터 다시 셈
    for key in list(H):
        pts = [p for p in H[key] if p[0] >= cut_h]
        if pts:
            H[key] = pts
        else:
            del H[key]
            continue
        days: dict = {}
        for p in pts:
            if day_of(p[0]) >= first_full:
                days.setdefault(day_of(p[0]), []).append(p)
        old = [x for x in D.get(key, []) if x[0] not in days]
        D[key] = sorted(old + [[dd, *_agg(v)] for dd, v in days.items()])
    cut_d = today - HIST_DAYS + 1
    for key in list(D):
        D[key] = [x for x in D[key] if x[0] >= cut_d]
        if not D[key]:
            del D[key]
    return hist


def pack(series: list) -> list:
    """[[몇 칸 전, 최저, 중간, 수량]…] (오래된 것부터) → [가장 오래된 칸, [최저…], [중간…], [수량…]].
    칸마다 하나씩(빈 칸 = null), 값은 앞 값과의 차이 — 시세가 그대로인 칸이 0 이 되어 압축이 잘 됨 (DECISIONS 2026-11-14)."""
    first = series[0][0]
    at = {x[0]: x for x in series}
    cols: list = [[], [], []]
    last = [0, 0, 0]
    for ago in range(first, -1, -1):
        x = at.get(ago)
        for c in range(3):
            if x is None:
                cols[c].append(None)
            else:
                cols[c].append(x[c + 1] - last[c])
                last[c] = x[c + 1]
    return [first, *cols]


def unpack(packed: list) -> list:
    """pack 의 반대 (앱 서버가 화면에 넘길 때)."""
    first, *cols = packed
    out, last = [], [0, 0, 0]
    for i in range(first + 1):
        if cols[0][i] is None:
            continue
        for c in range(3):
            last[c] += cols[c][i]
        out.append([first - i, *last])
    return out


def make_trend(hist: dict, hr: int, stamp: str) -> dict:
    """앱이 받는 요약: 48시간(1시간) · 7일(6시간) · 90일(하루). 시각은 「몇 칸 전」, pack 으로 줄임."""
    H, D, k = hist.get("h", {}), hist.get("d", {}), {}
    now6, nowd = six_of(hr), day_of(hr)
    for key in set(H) | set(D):
        pts = H.get(key, [])
        o = {}
        h = [[hr - p[0], *p[1:]] for p in pts if hr - p[0] < TREND_HOURS]
        if h:
            o["h"] = pack(h)
        six: dict = {}
        for p in pts:
            six.setdefault(six_of(p[0]), []).append(p)
        if six:
            o["s"] = pack([[now6 - b, *_agg(v)] for b, v in sorted(six.items())])
        days = {x[0]: x[1:] for x in D.get(key, [])}
        today = [p for p in pts if day_of(p[0]) == nowd]
        if today:
            days[nowd] = _agg(today)                    # 오늘은 지금까지 기록으로
        if days:
            o["d"] = pack([[nowd - dd, *v] for dd, v in sorted(days.items())])
        if o:
            k[key] = json.dumps(o, separators=(",", ":"))
    return {"updated": stamp, "now": hr, "start": hist.get("start", hr), "k": k}


def read_gz(p: Path) -> dict:
    return json.loads(gzip.decompress(p.read_bytes()).decode("utf-8")) if p.is_file() else {}


def write_gz(p: Path, obj) -> None:
    p.write_bytes(gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9))


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "auction"
    region = sys.argv[2] if len(sys.argv) > 2 else "kr"
    d = out / region
    d.mkdir(parents=True, exist_ok=True)
    api = Api(region)
    t0 = time.time()
    st, rs = api.get("/data/wow/ruleset/index", "dynamic-forever-{r}")
    rulesets = [r["id"] for r in (rs or {}).get("rulesets", [])]
    if not rulesets:
        raise SystemExit(f"규칙세트 없음 ({st})")
    rid = rulesets[0]                                  # 2026-11-13: 규칙세트마다 같은 경매장 자료
    st, hs = api.get(f"/data/wow/ruleset/{rid}/auctions/", "dynamic-forever-{r}")
    houses = [(h["id"], h.get("name", "")) for h in (hs or {}).get("auctions", [])]
    cache_p = d / "items.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.is_file() else {}
    suf_p = d / "suffix.json"
    suf = json.loads(suf_p.read_text(encoding="utf-8")) if suf_p.is_file() else {}
    all_keys = set()
    rnames = {}
    for r in (rs or {}).get("rulesets", []):
        _, rd = api.get(f"/data/wow/ruleset/{r['id']}", "dynamic-forever-{r}")
        rnames[r["id"]] = (rd or {}).get("name", "")
    index, all_ids = [], set()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    hr = int(time.time() // 3600)
    for hid, name in houses:
        st1, a = api.get(f"/data/wow/ruleset/{rid}/auctions/{hid}", "dynamic-forever-{r}")
        st2, c = api.get(f"/data/wow/ruleset/{rid}/auctions/{hid}/commodities", "dynamic-forever-{r}")
        au, co = (a or {}).get("auctions", []), (c or {}).get("auctions", [])
        rows = summarize(au, co)
        if not rows:
            continue
        all_ids.update(r[0] for r in rows)
        cols = ["id", "min", "qty", "listings", "median", "ladder"]
        (d / f"house-{hid}.json").write_text(json.dumps(
            {"house": hid, "name": name, "updated": stamp, "cols": cols, "items": rows},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        rows2 = summarize(au, co, split=True)
        all_keys.update(r[0] for r in rows2)
        hp = d / f"hist-{hid}.json.gz"
        hist = update_history(read_gz(hp), rows2, hr)
        write_gz(hp, hist)
        write_gz(d / f"trend-{hid}.json.gz", make_trend(hist, hr, stamp))
        (d / f"house2-{hid}.json").write_text(json.dumps(
            {"house": hid, "name": name, "updated": stamp, "cols": cols, "items": rows2},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        grp = HOUSE_GROUP.get(hid)
        rs_id = GROUP_RULESET.get(grp)
        fac = next((v for k, v in FACTION.items() if name.startswith(k)), "")
        index.append({"house": hid, "name": name, "faction": fac, "ruleset": rs_id, "ruleset_name": rnames.get(rs_id, ""),
                      "items": len(rows), "listings": sum(r[3] for r in rows)})
    found = lookup_names(api, sorted(all_ids), cache)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    t1 = time.time()
    sfound = lookup_suffixes(sorted(all_keys), suf)
    suf_p.write_text(json.dumps(suf, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"무작위 효과 {sum(1 for k in all_keys if ':' in k)}가지 (새로 찾음 {sfound}, {time.time() - t1:.0f}초)")
    (d / "index.json").write_text(json.dumps({"region": region, "updated": stamp, "houses": index,
                                              "source": "Blizzard Battle.net API (WoW Forever)"},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    named = sum(1 for i in all_ids if (cache.get(str(i)) or [""])[0])
    print(f"경매장 {len(index)}개, 아이템 {len(all_ids)}종 (이름 {named}종, 새로 찾음 {found}), 호출 {api.calls}번, {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
