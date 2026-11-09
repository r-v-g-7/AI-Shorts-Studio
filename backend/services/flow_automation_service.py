import asyncio
import logging
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from backend.config import settings
from backend.database.repository import JobRepository

logger = logging.getLogger(__name__)

CHROME_CDP_LAUNCH_INSTRUCTIONS = (
    "Chrome remote debugging is not reachable at {cdp_url}. "
    "To enable hands-free Google Flow automation, start Chrome with:\n"
    '  chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\ChromeDebug"\n'
    "and sign in to https://flow.google.com (or https://labs.google/fx/tools/flow). "
    "Job has been placed in VISUALS_PENDING so you can either launch Chrome on port 9222 and click Resume, "
    "or drop clips into your Downloads folder for automatic intake."
)


class CDPConnectionError(RuntimeError):
    """Raised when Playwright cannot connect to Chrome over CDP on port 9222."""
    pass


class FlowAutomationService:
    """
    Chrome DevTools Protocol (CDP) Playwright Automation Service for Google Flow.
    Connects to an authenticated Chrome session on port 9222, locates or opens
    Google Flow, submits each scene's 9:16 visual prompt, waits for video rendering,
    and saves/downloads scene_01.mp4 .. scene_04.mp4 into the Downloads folder
    (and/or job workspace) for automatic pipeline completion.
    """

    FLOW_URL_PATTERNS = ("flow.google", "labs.google/fx/tools/flow", "labs.google/fx")
    DEFAULT_FLOW_URL = "https://flow.google.com"

    PROMPT_SELECTORS = [
        'textarea[placeholder*="What would you like to create" i]',
        'textarea[placeholder*="Describe" i]',
        'textarea[placeholder*="prompt" i]',
        'textarea[aria-label*="prompt" i]',
        'div[contenteditable="true"][role="textbox"]',
        'textarea',
    ]

    ASPECT_TRIGGER_SELECTORS = [
        'button[aria-label*="Aspect" i]',
        'button[aria-label*="Settings" i]',
        'button[aria-label*="Tune" i]',
        'button[aria-label*="Ratio" i]',
        'button:has-text("16:9")',
        'button:has-text("9:16")',
        'button:has-text("Aspect ratio")',
        '[data-testid*="aspect" i]',
    ]

    PORTRAIT_OPTION_SELECTORS = [
        'button:has-text("9:16")',
        '[role="option"]:has-text("9:16")',
        '[role="menuitem"]:has-text("9:16")',
        'button:has-text("Portrait")',
        '[role="option"]:has-text("Portrait")',
        '[aria-label*="9:16" i]',
        '[aria-label*="Portrait" i]',
    ]

    SUBMIT_SELECTORS = [
        'button[aria-label*="Generate" i]',
        'button[aria-label*="Create" i]',
        'button[aria-label*="Submit" i]',
        'button[aria-label*="Send" i]',
        'button:has-text("Generate")',
        'button:has-text("Create")',
        'button[type="submit"]',
    ]

    DOWNLOAD_BUTTON_SELECTORS = [
        'button[aria-label*="Download" i]',
        'a[aria-label*="Download" i]',
        'button:has-text("Download")',
        'a[download]',
        '[data-testid*="download" i]',
    ]

    def __init__(
        self,
        cdp_url: Optional[str] = None,
        flow_url: Optional[str] = None,
        scene_timeout_seconds: Optional[int] = None,
        downloads_dir: Optional[Path] = None,
    ):
        self.cdp_url = (cdp_url or settings.CDP_DEBUGGING_URL or "http://127.0.0.1:9222").rstrip("/")
        self.flow_url = flow_url or getattr(settings, "FLOW_URL", self.DEFAULT_FLOW_URL) or self.DEFAULT_FLOW_URL
        self.scene_timeout_seconds = scene_timeout_seconds or getattr(settings, "FLOW_SCENE_TIMEOUT_SECONDS", 240)
        self.downloads_dir = Path(downloads_dir or settings.DOWNLOADS_DIR)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @classmethod
    def is_cdp_reachable(cls, cdp_url: Optional[str] = None, timeout: float = 2.5) -> bool:
        """Fast pre-check to see if Chrome remote debugging port is listening."""
        target = (cdp_url or settings.CDP_DEBUGGING_URL or "http://127.0.0.1:9222").rstrip("/")
        try:
            req = urllib.request.Request(f"{target}/json/version")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def connect_to_browser(self):
        """
        Connects to Chrome over CDP on http://127.0.0.1:9222.
        Locates an existing tab matching flow.google.com (or labs.google/fx/tools/flow).
        If not open, opens flow.google.com in a new tab.
        """
        if not self.is_cdp_reachable(self.cdp_url):
            msg = CHROME_CDP_LAUNCH_INSTRUCTIONS.format(cdp_url=self.cdp_url)
            logger.warning(msg)
            raise CDPConnectionError(msg)

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("Playwright is not installed. Run: pip install playwright") from e

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            msg = CHROME_CDP_LAUNCH_INSTRUCTIONS.format(cdp_url=self.cdp_url) + f" (Details: {e})"
            logger.warning(msg)
            raise CDPConnectionError(msg) from e

        contexts = self._browser.contexts
        if not contexts:
            self._context = await self._browser.new_context(accept_downloads=True)
        else:
            self._context = contexts[0]

        # Search all open tabs for an existing Google Flow tab
        matched_page = None
        for ctx in contexts:
            for page in ctx.pages:
                url_lower = (page.url or "").lower()
                if any(pat in url_lower for pat in self.FLOW_URL_PATTERNS):
                    matched_page = page
                    self._context = ctx
                    logger.info(f"[FlowCDP] Found existing Google Flow tab: {page.url}")
                    break
            if matched_page:
                break

        if matched_page is None:
            logger.info(f"[FlowCDP] No open Google Flow tab found. Opening {self.DEFAULT_FLOW_URL} in a new tab...")
            matched_page = await self._context.new_page()
            try:
                await matched_page.goto(self.DEFAULT_FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as nav_err:
                logger.warning(f"[FlowCDP] Navigation to {self.DEFAULT_FLOW_URL} reported: {nav_err}")

        self._page = matched_page
        try:
            await self._page.bring_to_front()
        except Exception:
            pass

        return self._page

    async def disconnect(self):
        """Cleanly disconnects the Playwright CDP client without closing the user's Chrome browser."""
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"[FlowCDP] Disconnect cleanup note: {e}")
        finally:
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None

    async def inspect_browser_state(self) -> Dict[str, Any]:
        """
        Connects via CDP, lists all open tabs, and validates DOM accessibility on the active Flow tab.
        Used by diagnostics and test_cdp_connection.py.
        """
        page = await self.connect_to_browser()
        tabs_info = []
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                try:
                    t_title = await p.title()
                except Exception:
                    t_title = "<untitled>"
                tabs_info.append({"url": p.url, "title": t_title})

        dom_info = await page.evaluate(
            """() => ({
                readyState: document.readyState,
                title: document.title,
                url: window.location.href,
                textareas: document.querySelectorAll('textarea').length,
                buttons: document.querySelectorAll('button').length,
                videos: document.querySelectorAll('video').length
            })"""
        )
        return {
            "cdp_url": self.cdp_url,
            "connected": True,
            "open_tabs_count": len(tabs_info),
            "open_tabs": tabs_info,
            "flow_tab": dom_info,
        }

    async def _find_prompt_locator(self, page):
        """Locates the visible prompt input/textarea on Google Flow."""
        for selector in self.PROMPT_SELECTORS:
            loc = page.locator(selector).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    async def _ensure_portrait_aspect_ratio(self, page):
        """
        Ensures the aspect ratio is set to 9:16 Portrait.
        Clicks the tune/settings/aspect button if present and selects 9:16 Portrait.
        """
        try:
            # First check if a 9:16 option is already directly visible
            for opt_sel in self.PORTRAIT_OPTION_SELECTORS:
                opt = page.locator(opt_sel).first
                if await opt.count() > 0 and await opt.is_visible():
                    await opt.click(timeout=2000)
                    logger.info(f"[FlowCDP] Selected 9:16 Portrait via '{opt_sel}'")
                    return True

            # Otherwise click the settings / tune / aspect trigger button
            for trig_sel in self.ASPECT_TRIGGER_SELECTORS:
                trig = page.locator(trig_sel).first
                if await trig.count() > 0 and await trig.is_visible():
                    await trig.click(timeout=2500)
                    await page.wait_for_timeout(400)
                    for opt_sel in self.PORTRAIT_OPTION_SELECTORS:
                        opt = page.locator(opt_sel).first
                        if await opt.count() > 0 and await opt.is_visible():
                            await opt.click(timeout=2500)
                            logger.info(f"[FlowCDP] Set aspect ratio to 9:16 Portrait via '{trig_sel}' -> '{opt_sel}'")
                            await page.wait_for_timeout(300)
                            return True
        except Exception as e:
            logger.debug(f"[FlowCDP] Aspect ratio selector check skipped/non-fatal: {e}")
        return False

    async def _submit_prompt(self, page, prompt_locator):
        """Clicks the Generate/Submit button or presses Enter on the prompt input."""
        for sel in self.SUBMIT_SELECTORS:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                    await btn.click(timeout=3000)
                    logger.info(f"[FlowCDP] Clicked submit button matching '{sel}'")
                    return
            except Exception:
                continue

        logger.info("[FlowCDP] Submit button not matched directly; pressing Enter inside prompt box")
        await prompt_locator.press("Enter")

    async def generate_and_download_scene(
        self,
        prompt: str,
        scene_index: int,
        output_path: Optional[Path] = None,
        job_id: Optional[str] = None,
    ) -> Path:
        """
        Automates generating and downloading a single scene in Google Flow:
        1. Locates the prompt textarea ("What would you like to create?").
        2. Fills in the visual prompt for scene_index.
        3. Ensures aspect ratio is set to 9:16 Portrait.
        4. Clicks submit/generate.
        5. Waits for generation to complete (monitors for new video card / download button).
        6. Triggers download to Downloads/scene_0X.mp4 (and copies to output_path if given).
        """
        if self._page is None:
            await self.connect_to_browser()

        page = self._page
        scene_num = scene_index + 1
        target_download_file = self.downloads_dir / f"scene_{scene_num:02d}.mp4"

        if job_id:
            JobRepository.add_log(
                job_id,
                "INFO",
                "FLOW_AUTOMATION",
                f"GENERATING_FLOW_VISUALS (Scene {scene_num}/4): Submitting prompt to Google Flow..."
            )

        # Wait briefly for UI readiness
        await page.wait_for_timeout(1000)

        prompt_locator = await self._find_prompt_locator(page)
        if prompt_locator is None:
            raise RuntimeError(
                f"Could not locate the Google Flow prompt input on {page.url}. "
                "Ensure you are signed in to Google Flow in the Chrome session on port 9222."
            )

        # Snapshot current video count and download button count before submitting
        prev_video_count = await page.locator("video").count()
        prev_dl_count = 0
        for sel in self.DOWNLOAD_BUTTON_SELECTORS:
            prev_dl_count += await page.locator(sel).count()

        # Ensure 9:16 Portrait aspect ratio
        await self._ensure_portrait_aspect_ratio(page)

        # Fill in the scene prompt
        await prompt_locator.click(timeout=5000)
        await prompt_locator.fill(prompt)
        await page.wait_for_timeout(300)

        # Submit generation
        await self._submit_prompt(page, prompt_locator)
        logger.info(f"[FlowCDP] Scene {scene_num} prompt submitted. Waiting up to {self.scene_timeout_seconds}s for generation...")

        # Poll for new video element or download button or direct file in Downloads
        start_ts = time.time()
        downloaded_path: Optional[Path] = None

        while time.time() - start_ts < self.scene_timeout_seconds:
            await page.wait_for_timeout(3000)

            # Check if a new download button appeared and trigger download
            for sel in self.DOWNLOAD_BUTTON_SELECTORS:
                loc = page.locator(sel)
                count = await loc.count()
                if count > 0:
                    last_btn = loc.first
                    try:
                        if await last_btn.is_visible() and (count > prev_dl_count or (await page.locator("video").count()) > prev_video_count):
                            async with page.expect_download(timeout=30000) as download_info:
                                await last_btn.click(timeout=5000)
                            download = await download_info.value
                            await download.save_as(str(target_download_file))
                            downloaded_path = target_download_file
                            logger.info(f"[FlowCDP] Downloaded Scene {scene_num} via download button -> {target_download_file}")
                            break
                    except Exception as dl_err:
                        logger.debug(f"[FlowCDP] Download button attempt note: {dl_err}")

            if downloaded_path and downloaded_path.exists() and downloaded_path.stat().st_size > 1000:
                break

            # Also check if a new <video src="https://..."> element appeared with a downloadable URL
            curr_video_count = await page.locator("video").count()
            if curr_video_count > prev_video_count:
                video_src = await page.locator("video").first.get_attribute("src")
                if video_src and video_src.startswith("http"):
                    try:
                        req = urllib.request.Request(video_src, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            data = resp.read()
                            if len(data) > 1000:
                                target_download_file.write_bytes(data)
                                downloaded_path = target_download_file
                                logger.info(f"[FlowCDP] Downloaded Scene {scene_num} from video src -> {target_download_file}")
                                break
                    except Exception as src_err:
                        logger.debug(f"[FlowCDP] Direct video src fetch note: {src_err}")

        if not downloaded_path or not downloaded_path.exists() or downloaded_path.stat().st_size < 1000:
            raise TimeoutError(
                f"Google Flow scene {scene_num} generation timed out after {self.scene_timeout_seconds}s on {page.url}."
            )

        if output_path and output_path.resolve() != downloaded_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(downloaded_path), str(output_path))
            return output_path

        return downloaded_path

    async def generate_all_scenes(
        self,
        prompts: List[str],
        job_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
        on_scene_start: Optional[Callable[[int, int], None]] = None,
    ) -> List[Path]:
        """
        Runs through all scenes (e.g. 4 scenes) sequentially with safety timeouts and error handling.
        """
        results: List[Path] = []
        total = len(prompts)
        try:
            await self.connect_to_browser()
            for idx, prompt in enumerate(prompts):
                if on_scene_start:
                    on_scene_start(idx + 1, total)
                out_file = (output_dir / f"scene_{idx + 1:02d}.mp4") if output_dir else None
                clip_path = await self.generate_and_download_scene(
                    prompt=prompt,
                    scene_index=idx,
                    output_path=out_file,
                    job_id=job_id,
                )
                results.append(clip_path)
            return results
        finally:
            await self.disconnect()

    def run_scene_sync(
        self,
        prompt: str,
        scene_index: int,
        output_path: Optional[Path] = None,
        job_id: Optional[str] = None,
    ) -> Path:
        """Synchronous wrapper for generate_and_download_scene."""
        async def _runner():
            try:
                await self.connect_to_browser()
                return await self.generate_and_download_scene(
                    prompt=prompt,
                    scene_index=scene_index,
                    output_path=output_path,
                    job_id=job_id,
                )
            finally:
                await self.disconnect()

        return self._run_coro_sync(_runner())

    @staticmethod
    def _run_coro_sync(coro):
        """Executes an async coroutine synchronously even if an event loop is running in another thread."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
        return asyncio.run(coro)
