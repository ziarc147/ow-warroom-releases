"""포에버 경매장 시세 정리 (GitHub 자동 작업이 1시간마다 실행 — 개발자 PC에서도 시험 가능).

  python tools/auction_snapshot.py [출력 폴더=dist/auction] [지역=kr]

열쇠: 환경 변수 BNET_CLIENT_ID / BNET_CLIENT_SECRET (GitHub Secrets), 없으면 app/config/battlenet_client.json.
표준 라이브러리만 씀.

출력 (<출력>/<지역>/):
  index.json            경매장 목록·갱신 시각·매물 수
  house-<id>.json       아이템별 [id, 최저 개당가(동), 총 수량, 매물 수, 중간 개당가, [[가격, 수량], … 싼 순 최대 8단]]
  items.json            아이템 이름표 {id: [이름, 품질, 분류, 아이콘 번호]} — 한 번 찾은 것은 다음에 다시 씀(캐시)

경매장 묶음 = 서버군 (2026-11-13 확인): 경매장 id 110~125 는 「서버군마다 얼라이언스·호드·검은바다」.
  묶음 1(110·113·116)=PvP, 2(111·114·117)=일반, 3(112·115·118)=항상 빔, 4(119~121)=롤플레잉, 5(123~125)=롤플레잉 PvP
  (미국·유럽 서버군 4개 → 1·2·4·5 묶음에 매물, 한국·대만 2개 → 1·2. 블리자드 자료에 서버군 이름이 직접 붙지 않아 순서로 맞춤)
"""
from __future__ import annotations

import base64
import json
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


def summarize(auctions: list, commodities: list) -> list:
    """아이템별 [id, 최저 개당가, 총 수량, 매물 수, 중간 개당가]. 장비는 buyout/수량, 재료는 unit_price."""
    per: dict[int, list] = {}
    for a in auctions:
        q = a.get("quantity") or 1
        price = a.get("buyout") or a.get("unit_price")
        if not price:
            continue                                  # 즉시 구매가 없는 입찰 전용
        per.setdefault(a["item"]["id"], []).append((price // q if a.get("buyout") else price, q))
    for a in commodities:
        if a.get("unit_price"):
            per.setdefault(a["item"]["id"], []).append((a["unit_price"], a.get("quantity") or 1))
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


def lookup_names(api: Api, ids: list, cache: dict, budget: int = 6000) -> int:
    todo = [i for i in ids if str(i) not in cache or len(cache[str(i)]) < 4][:budget]   # 예전 캐시(아이콘 없음)도 다시

    def icon(i):
        st, js = api.get(f"/data/wow/media/item/{i}", "static-forever-{r}")
        for a in (js or {}).get("assets", []):
            if a.get("key") == "icon":
                return a.get("value", "").rsplit("/", 1)[-1].split(".")[0]      # forever-kr/icons/56/<번호>.jpg
        return ""

    def one(i):
        for ns in ITEM_NS:
            st, js = api.get(f"/data/wow/item/{i}", ns)
            if st == 200 and js and js.get("name"):
                return i, [js["name"], QUALITY.get((js.get("quality") or {}).get("type"), 1),
                           (js.get("item_class") or {}).get("name", ""), icon(i)]
            time.sleep(0.02)
        return i, None
    with ThreadPoolExecutor(max_workers=16) as ex:          # 블리자드 한도 초당 100회 아래로
        for i, v in ex.map(one, todo):
            cache[str(i)] = v or ["", 1, "", ""]
    return len(todo)


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
    rnames = {}
    for r in (rs or {}).get("rulesets", []):
        _, rd = api.get(f"/data/wow/ruleset/{r['id']}", "dynamic-forever-{r}")
        rnames[r["id"]] = (rd or {}).get("name", "")
    index, all_ids = [], set()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for hid, name in houses:
        st1, a = api.get(f"/data/wow/ruleset/{rid}/auctions/{hid}", "dynamic-forever-{r}")
        st2, c = api.get(f"/data/wow/ruleset/{rid}/auctions/{hid}/commodities", "dynamic-forever-{r}")
        rows = summarize((a or {}).get("auctions", []), (c or {}).get("auctions", []))
        if not rows:
            continue
        all_ids.update(r[0] for r in rows)
        (d / f"house-{hid}.json").write_text(json.dumps(
            {"house": hid, "name": name, "updated": stamp, "cols": ["id", "min", "qty", "listings", "median", "ladder"], "items": rows},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        grp = HOUSE_GROUP.get(hid)
        rs_id = GROUP_RULESET.get(grp)
        fac = next((v for k, v in FACTION.items() if name.startswith(k)), "")
        index.append({"house": hid, "name": name, "faction": fac, "ruleset": rs_id, "ruleset_name": rnames.get(rs_id, ""),
                      "items": len(rows), "listings": sum(r[3] for r in rows)})
    found = lookup_names(api, sorted(all_ids), cache)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (d / "index.json").write_text(json.dumps({"region": region, "updated": stamp, "houses": index,
                                              "source": "Blizzard Battle.net API (WoW Forever)"},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    named = sum(1 for i in all_ids if (cache.get(str(i)) or [""])[0])
    print(f"경매장 {len(index)}개, 아이템 {len(all_ids)}종 (이름 {named}종, 새로 찾음 {found}), 호출 {api.calls}번, {time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
