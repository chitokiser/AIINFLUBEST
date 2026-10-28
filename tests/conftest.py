import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from influencer_studio.config import load_settings  # noqa: E402
from influencer_studio.studio import Studio  # noqa: E402

HARIN = ("베트남 하노이에서 활동하는 27세 한국인 여성 AI 인플루언서를 생성해. 한국 음식, 여행, 패션을 주제로 콘텐츠를 "
         "제작하고, 밝고 친근한 말투를 사용해. 한국어와 베트남어를 사용하고 장기적으로 한국과 베트남의 문화 교류를 다루는 "
         "스토리를 만들어.")


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    # 테스트는 외부 모델 없이 결정적으로 동작해야 한다
    monkeypatch.setenv("AIS_LLM_PROVIDER", "template")
    monkeypatch.setenv("AIS_IMAGE_PROVIDER", "placeholder")
    monkeypatch.setenv("AIS_RUN_WORKER", "0")
    monkeypatch.setenv("AIS_OLLAMA_EMBED_MODEL", "")
    monkeypatch.setenv("AIS_FFMPEG", "ffmpeg-not-installed-for-tests")


@pytest.fixture
def make_studio(tmp_path):
    made = []

    def factory(sub="data", **kw):
        st = Studio(load_settings(tmp_path / sub), **kw)
        made.append(st)
        return st

    yield factory
    for st in made:
        st.close()


@pytest.fixture
def studio(make_studio):
    return make_studio()


def new_active(st, description=HARIN, name=None):
    res = st.influencers.create(description, {"name": name} if name else None)
    st.influencers.update(res["influencer_id"], status="ACTIVE")
    return res["influencer_id"]


def approved_story(st, iid, **kw):
    res = st.stories.generate(iid, **kw)
    st.stories.approve(res["story_id"])
    return res["story_id"]
