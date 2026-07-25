# 한국 사건사고 쇼츠 자동화 파이프라인

## 처음 설정하는 순서

1. 이 폴더 전체를 GitHub 새 리포지토리에 push 합니다.
2. `assets/character_refs/` 에 캐릭터 레퍼런스 이미지 4장을 넣습니다.
   - clerk_voice.png (알바생)
   - customer_taxi_voice.png (택시기사)
   - customer_student_voice.png (취준생)
   - customer_worker_voice.png (야간근무자)
3. (선택) `assets/bgm.mp3`, `assets/fonts/subtitle.ttf` 를 넣습니다.
4. 로컬에서 upload_video.py 를 한 번 실행해 YouTube OAuth 인증을 완료하고
   client_secret.json / token.json 을 만듭니다. (README 하단 참고)
5. `dashboard.html` 을 브라우저로 엽니다 (더블클릭). claude.ai 미리보기가 아니라
   실제 브라우저에서 여는 걸 추천합니다.
6. 대시보드에서 owner/repo, GitHub PAT을 입력하고 "연결 확인".
7. 체크리스트에서 빠진 시크릿을 대시보드에서 바로 입력/저장.
8. 준비가 끝나면 "지금 실행" 버튼.

## GitHub PAT 만들기 (fine-grained token 추천)
GitHub -> Settings -> Developer settings -> Fine-grained tokens -> Generate new token
- Repository access: 이 리포지토리만 선택
- Permissions:
  - Actions: Read and write
  - Secrets: Read and write
  - Contents: Read and write (파일 존재 확인용)

## YouTube OAuth 최초 인증 (1회, 로컬에서)
```
pip install -r requirements.txt
python upload_video.py   # 브라우저가 열리며 로그인/승인 요청
```
성공하면 이 폴더에 token.json 이 생깁니다. client_secret.json 은
Google Cloud Console에서 미리 다운로드해서 이 폴더에 넣어두세요.
