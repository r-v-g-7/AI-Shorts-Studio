import asyncio
import json
from backend.config import settings
from backend.database.connection import init_db
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.flow_automation_service import (
    FlowAutomationService,
    CDPConnectionError,
)


async def test_live_cdp_and_dom():
    print("=" * 80)
    print("PHASE 7 VERIFICATION: CHROME DEVTOOLS PROTOCOL (CDP) PLAYWRIGHT AUTOMATION")
    print("=" * 80)

    print(f"\n[1] Checking Configuration...")
    print(f"    CDP_DEBUGGING_URL:         {settings.CDP_DEBUGGING_URL}")
    print(f"    VIDEO_GENERATION_PROVIDER: {settings.VIDEO_GENERATION_PROVIDER}")
    assert settings.CDP_DEBUGGING_URL == "http://127.0.0.1:9222"
    assert settings.VIDEO_GENERATION_PROVIDER == "flow_browser"

    reachable = FlowAutomationService.is_cdp_reachable()
    print(f"    CDP Port 9222 Reachable:   {reachable}")

    service = FlowAutomationService()
    if reachable:
        print("\n[2] Connecting to live Chrome session on http://127.0.0.1:9222 via Playwright CDP...")
        try:
            state = await service.inspect_browser_state()
            print(f"[PASS] Connected to Chrome via CDP ({state['cdp_url']})!")
            print(f"[PASS] Open Tabs Detected: {state['open_tabs_count']}")
            for idx, tab in enumerate(state["open_tabs"], 1):
                print(f"       Tab {idx}: {tab['title']} -> {tab['url']}")

            flow_dom = state["flow_tab"]
            print(f"[PASS] Active Flow Tab DOM Validated:")
            print(f"       URL:        {flow_dom['url']}")
            print(f"       Title:      {flow_dom['title']}")
            print(f"       ReadyState: {flow_dom['readyState']}")
            print(f"       Textareas:  {flow_dom['textareas']}, Buttons: {flow_dom['buttons']}, Videos: {flow_dom['videos']}")
            assert flow_dom["readyState"] in ("interactive", "complete")
        finally:
            await service.disconnect()
    else:
        print("\n[2] Port 9222 is not currently open; verifying clean CDPConnectionError diagnostic...")
        try:
            await service.connect_to_browser()
            assert False, "Expected CDPConnectionError when port 9222 is closed"
        except CDPConnectionError as e:
            assert "--remote-debugging-port=9222" in str(e)
            print(f"[PASS] Clean diagnostic raised: {e}")

    print("\n[3] Testing Pipeline Safe Fallback to VISUALS_PENDING when CDP is unreachable...")
    init_db()
    job = PipelineController.create_new_job(
        topic="Ek chhota sa sher chaand ko pakadne ke liye oonche pahad par chadh gaya.",
        title="CDP Fallback Verification Short",
        visual_style=VisualStyle.TALL_TALE_3D,
    )
    job_id = job["id"]
    PipelineController.execute_script_stage(job_id)

    # Temporarily point CDP_DEBUGGING_URL to an closed port to test safe fallback
    orig_cdp = settings.CDP_DEBUGGING_URL
    try:
        settings.CDP_DEBUGGING_URL = "http://127.0.0.1:9299"
        clips = PipelineController.execute_video_generation_stage(
            job_id=job_id,
            provider_override="flow_browser",
        )
        assert clips == [], f"Expected empty clip list on fallback, got {clips}"
        updated_job = JobService.get_job(job_id)
        assert updated_job["status"] == JobStatus.VISUALS_PENDING.value, (
            f"Expected job status VISUALS_PENDING, got {updated_job['status']}"
        )
        logs = JobRepository.get_logs(job_id)
        assert any("--remote-debugging-port=9222" in (lg.get("message") or "") for lg in logs), (
            "Expected --remote-debugging-port=9222 launch instructions in job logs"
        )
        print(f"[PASS] Unreachable CDP safely placed job '{job_id[:8]}' into VISUALS_PENDING with launch instructions logged!")
    finally:
        settings.CDP_DEBUGGING_URL = orig_cdp

    print("\n" + "=" * 80)
    print("ALL CDP CONNECTION & FLOW AUTOMATION CHECKS PASSED (100% SUCCESS)!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_live_cdp_and_dom())
