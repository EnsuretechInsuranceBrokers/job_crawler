"""The visible browser for the commands you run on your own computer.

All of them share one saved profile (``~/.jobbot/browser``, or $JOBBOT_BROWSER_PROFILE), so a
sign-in made once - in any of them - is still there for the next job and the next run.
"""
import os
import sys

PROFILE_DIR = os.path.expanduser(os.environ.get("JOBBOT_BROWSER_PROFILE", "~/.jobbot/browser"))


def _exe():
    return os.environ.get("JOBBOT_CHROMIUM") or None


def open_profile(pw, locale="en-IN"):
    """A visible browser window using the saved profile. One window is reused for every job."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        return pw.chromium.launch_persistent_context(PROFILE_DIR, headless=False, viewport=None,
                                                     locale=locale, executable_path=_exe())
    except Exception as e:  # noqa: BLE001
        if "ProcessSingleton" in str(e) or "SingletonLock" in str(e) or "already in use" in str(e):
            sys.exit("The bot's browser profile is already open in another jobbot window. Close that one first, or\n"
                     "give this run its own profile: JOBBOT_BROWSER_PROFILE=~/.jobbot/browser2 (you sign in there once).")
        raise


def renderer(pw):
    """An invisible browser used only to turn resumes and letters into PDFs."""
    return pw.chromium.launch(executable_path=_exe())


def work_page(context):
    """The one tab the bot works in; extra tabs a site opened are closed, the window stays."""
    pages = context.pages
    page = pages[0] if pages else context.new_page()
    for extra in pages[1:]:
        try:
            extra.close()
        except Exception:  # noqa: BLE001
            pass
    return page
