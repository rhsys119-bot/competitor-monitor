# 경쟁사 신규 컨텐츠

TYM · LS엠트론/LS트랙터의 공개 공식채널을 모아 보는 작은 웹페이지입니다.

- 최근 14일을 최신순으로 표시하고, 과거 자료를 펼치거나 검색합니다.
- 읽음 상태는 방문자의 브라우저에만 저장합니다.
- 공식채널 목록과 수집 실패 여부를 확인할 수 있습니다.
- 영상·이미지 원본은 보관하지 않습니다. 제목·게시일·링크·짧은 설명만 저장합니다.
- GitHub Pages가 화면을 제공하고 GitHub Actions가 매시간 공식채널을 확인합니다.

이 저장소와 웹페이지, 수집 목록은 공개됩니다. 로그인이나 사내 전용 접근 제한은 없습니다. 회사 내부 자료, 계정 비밀번호, API 비밀키를 올리지 마세요. 이 프로젝트는 각 경쟁사의 공식 서비스가 아니며 원문은 각 공식채널에서 확인할 수 있습니다.

## 처음 설정하기

1. GitHub에 로그인하고 `competitor-monitor`라는 **Public** 저장소를 만듭니다. 기본 브랜치는 `main`을 사용합니다.
2. 이 프로젝트의 파일과 폴더를 저장소 최상위에 올립니다. `web` 폴더 안에 `index.html`과 `data/news.json`이 들어 있어야 합니다. 기존 PC 서버 폴더 전체를 올리는 방식이 아닙니다.
3. `.github/workflows/monitor.yml`도 올립니다. 브라우저 업로드에서 이 폴더가 빠졌다면 **Add file → Create new file**을 선택하고 파일명을 `.github/workflows/monitor.yml`로 입력한 뒤, 제공된 자동수집 설정 내용을 붙여 넣습니다.
4. 저장소 **Settings → Pages → Build and deployment → Source**에서 **GitHub Actions**를 선택합니다.
5. **Actions → Collect and publish → Run workflow → main → Run workflow**를 누릅니다.
6. 작업이 성공하면 **Settings → Pages → Visit site**에서 공개 주소를 확인합니다. 보통 `https://깃허브아이디.github.io/competitor-monitor/` 형태입니다.

## 사용하기

웹페이지의 **최신 결과 불러오기**는 이미 수집되어 공개된 결과를 다시 읽습니다. 공식채널을 즉시 재수집하려면 저장소의 **Actions → Collect and publish → Run workflow**를 사용합니다. 동료들은 공개 웹주소로 접속하면 되며 GitHub 가입은 필요 없습니다.

GitHub의 예약 실행은 지연되거나 누락될 수 있어 정확한 매시각 실행을 보장하지 않습니다. 페이지에 표시된 마지막 수집 시각을 확인하세요. 공개 저장소가 장기간 비활성 상태이면 예약 실행이 중단될 수 있습니다. 필요하면 Actions에서 해당 작업을 다시 활성화합니다.

현재 12개 채널은 자동 수집 대상이고, 인스타그램 등 6개는 공식채널 링크를 통해 직접 확인합니다. 클라우드의 접속 환경에서 일부 공식 사이트가 요청을 막으면 해당 채널의 오류와 이전 자료가 함께 표시됩니다. 오류를 새 글 없음으로 표시하지 않습니다.

## 무료 범위

공개 저장소의 표준 GitHub 호스팅 실행기와 Pages를 사용하는 구성입니다. 별도 Supabase·Netlify·도메인 구매가 필요 없습니다. 무료 정책과 서비스 제한은 변경될 수 있습니다. GitHub Pages의 사이트 크기·전송량 제한과 Actions의 이용 조건이 적용됩니다.

- [GitHub Pages 자동 배포 안내](https://docs.github.com/en/get-started/start-your-journey/deploying-your-website-automatically)
- [GitHub Pages 사용량 제한](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- [GitHub Actions 비용 안내](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [예약 실행 제한](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 개발용 메모

`python cloud_collect.py`는 공개 데이터 파일의 과거 자료를 유지하면서 채널별 결과를 갱신합니다. 수집 중 사용하는 SQLite는 임시 폴더에만 존재합니다. `server.py`는 기존 자료 검증·중복 처리 로직을 재사용하기 위한 모듈이며, GitHub에서 PC용 웹서버를 실행하지 않습니다.

설정과 수집기 코드는 저장소에, 화면에 필요한 파일만 `web/`에 둡니다. GitHub Pages에는 `web/`만 배포합니다. 비밀번호·개인별 읽음 상태·PC 경로·IP 주소·실행 토큰은 배포 데이터에 포함하지 않습니다.
