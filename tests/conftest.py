from maits import runtime


def pytest_sessionfinish(session, exitstatus):
    # Twisted's worker threads are non-daemon; without this pytest never exits.
    runtime._reactor.fireSystemEvent("shutdown")
