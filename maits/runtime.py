"""One event loop for everything.

The cTrader SDK is built on Twisted, while FastAPI/uvicorn are asyncio. Rather than juggling
threads, Twisted's asyncio reactor makes both share a single asyncio loop.

The SDK installs Twisted's *default* reactor the moment `ctrader_open_api` is imported, so this
module must be imported first (maits.ctrader does that). `run()` drives the loop.
"""
import asyncio
import sys

from twisted.internet import asyncioreactor

if "twisted.internet.reactor" in sys.modules:
    from twisted.internet import reactor as _reactor

    if not isinstance(_reactor, asyncioreactor.AsyncioSelectorReactor):
        raise RuntimeError(
            "A non-asyncio Twisted reactor is already installed. Import `maits.runtime` "
            "before anything that imports ctrader_open_api or twisted.internet.reactor."
        )
    loop = asyncio.new_event_loop()  # pragma: no cover - unusual path
else:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    asyncioreactor.install(loop)
    from twisted.internet import reactor as _reactor

    # Fire Twisted's startup events (thread pool for DNS, ...) without letting it take over
    # the loop or the signal handlers (uvicorn wants those).
    _reactor.startRunning(installSignalHandlers=False)


def run(coro):
    """Run a coroutine to completion on the shared loop, then shut Twisted down.

    Once per process: Twisted reactors can't be restarted. The shutdown matters because Twisted's
    worker threads are non-daemon - without it the interpreter never exits.
    """
    try:
        return loop.run_until_complete(coro)
    finally:
        if _reactor.running:
            _reactor.fireSystemEvent("shutdown")  # stops the thread pool, closes connections
