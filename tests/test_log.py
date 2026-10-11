import logging
import re

import pytest

from agent_loop import log


@pytest.fixture
def lines(capsys):
    def read():
        return capsys.readouterr().err.splitlines()

    return logging.getLogger("agent_loop.test"), read


@pytest.fixture(autouse=True)
def reset_handlers():
    # capsys replaces sys.stderr only for the test body, so the handler is created there
    yield
    logging.getLogger(log.LOGGER_NAME).handlers.clear()


def test_line_without_scope(lines):
    logger, read = lines
    log.setup()

    logger.info("started")

    assert re.fullmatch(r"\d\d:\d\d:\d\d started", read()[0])


def test_nested_scopes_extend_and_restore(lines):
    logger, read = lines
    log.setup()

    with log.scope(run="3f2a"):
        logger.info("a")
        with log.scope(task="2/7", stage="implement"):
            logger.info("b")
            with log.scope(stage="gating"):
                logger.info("c")
        logger.info("d")
    logger.info("e")

    tails = [line.split(" ", 1)[1] for line in read()]
    assert tails == [
        "[run 3f2a] a",
        "[run 3f2a] [task 2/7] [implement] b",
        "[run 3f2a] [task 2/7] [gating] c",
        "[run 3f2a] d",
        "e",
    ]


def test_level_shown_from_warning(lines):
    logger, read = lines
    log.setup()

    logger.warning("careful")
    logger.debug("hidden")

    output = read()
    assert len(output) == 1
    assert output[0].endswith(" WARNING careful")


def test_verbose_shows_debug(capsys):
    log.setup(verbose=True)

    logging.getLogger("agent_loop.test").debug("details")

    assert capsys.readouterr().err.endswith("details\n")
