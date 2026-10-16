import threading

from agent_loop.files import write_atomic


def test_threads_of_one_process_write_one_file(tmp_path):
    path = tmp_path / "stop"
    errors = []

    def write(reason):
        try:
            for _ in range(200):
                write_atomic(path, reason)
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(r,)) for r in ("interrupt", "timeout")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert path.read_text() in ("interrupt", "timeout")
    assert [p.name for p in tmp_path.iterdir()] == ["stop"]
