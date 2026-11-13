# 오그리마와이번스 전투정보실 — 배포

와우 포에버 애드온 설정·폰트를 내 구글 드라이브에 보관하는 윈도우 앱의 **배포 전용 저장소**예요.
(소스 코드는 비공개 저장소에서 관리)

- 다운로드: [Releases](https://github.com/ziarc147/ow-warroom-releases/releases)
- 소개: https://ziarc147.github.io/ow-warroom-releases/
- 개인정보처리방침: https://ziarc147.github.io/ow-warroom-releases/privacy.html
- 문의: [Issues](https://github.com/ziarc147/ow-warroom-releases/issues)

## 경매장 시세 자료 (자동)
`.github/workflows/auction.yml` 이 1시간마다 블리자드 Battle.net API(WoW 포에버, 한국)에서 경매장 자료를 받아
`scripts/auction_snapshot.py` 로 정리한 파일을 [auction-data](https://github.com/ziarc147/ow-warroom-releases/releases/tag/auction-data) 릴리스에 올립니다.
앱은 이 파일만 받아 보여 주고, 블리자드 열쇠는 이 저장소의 Secrets(`BNET_CLIENT_ID`, `BNET_CLIENT_SECRET`)에만 있습니다.

- `kr-index.json` 경매장 목록(서버군·진영·매물 수)·갱신 시각
- `kr-house-<id>.json` 아이템별 최저가·수량·매물 수·중간가·가격대별 매물
- `kr-items.json` 아이템 이름·품질·분류·아이콘 번호
