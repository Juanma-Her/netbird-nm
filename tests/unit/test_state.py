import threading
import time

from netbird_nm.state import StateDir


def test_resume_flag(tmp_path):
    st = StateDir(tmp_path / "s")
    assert not st.has_resume()
    st.set_resume("uuid-1")
    assert st.has_resume()
    assert st.pop_resume() == "uuid-1"
    assert not st.has_resume()
    assert st.pop_resume() is None


def test_clear_resume_is_idempotent(tmp_path):
    st = StateDir(tmp_path)
    st.clear_resume()
    st.set_resume("u")
    st.clear_resume()
    assert not st.has_resume()


def test_down_pending_and_wait(tmp_path, clock):
    st = StateDir(tmp_path)
    assert st.wait_down_done(timeout=1, sleep=clock)
    st.mark_down_pending()
    assert st.down_pending()
    assert st.wait_down_done(timeout=1, sleep=clock) is False
    assert clock.slept == 1.0


def test_wait_returns_when_down_finishes(tmp_path):
    st = StateDir(tmp_path)
    st.mark_down_pending()
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 3:
            st.clear_down_pending()
    assert st.wait_down_done(timeout=10, sleep=sleep)
    assert len(calls) == 3


def test_keep_flag(tmp_path):
    st = StateDir(tmp_path)
    assert not st.keep()
    st.set_keep()
    assert st.keep()


def test_lock_serializes(tmp_path):
    st = StateDir(tmp_path)
    order = []

    def worker(name, hold):
        with st.lock():
            order.append(f"{name}-in")
            time.sleep(hold)
            order.append(f"{name}-out")

    t1 = threading.Thread(target=worker, args=("a", 0.2))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=("b", 0))
    t2.start()
    t1.join()
    t2.join()
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_wipe(tmp_path):
    st = StateDir(tmp_path / "s")
    st.set_resume("u")
    st.set_keep()
    st.mark_down_pending()
    with st.lock():
        pass
    st.wipe()
    assert not (tmp_path / "s").exists()
