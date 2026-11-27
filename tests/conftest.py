import os

# API 테스트가 업로드할 때마다 AI 모델을 다시 학습하지 않게(30초씩 걸리고 DB에 모델 버전이 쌓인다)
os.environ.setdefault("PSD_AUTO_RETRAIN", "0")

# 연동 파일 감시 스레드를 켜지 않는다(TestClient를 with로 써도). 감시는 tests/test_link.py가 직접 돌려 본다
os.environ.setdefault("PSD_LINK_WATCH", "0")
