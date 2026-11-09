from backend.main import (
    health_check,
    create_new_job,
    get_job_by_id,
    get_job_manifest,
    get_job_logs,
    list_all_jobs
)
from backend.schemas.job_manifest import JobCreateRequest, VisualStyle

def test_phase1():
    # 1. Health check
    h = health_check()
    assert h["status"] == "healthy", f"Health check failed: {h}"
    print("[PASS] Health check passed:", h["status"])

    # 2. Create Job
    req = JobCreateRequest(
        title="The Secret of Mariana Trench",
        topic="Deep sea exploration and pressure adapted organisms",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job = create_new_job(req)
    job_id = job["id"]
    assert job["title"] == req.title
    print("[PASS] Job created successfully, ID:", job_id)

    # 3. List Jobs
    jobs = list_all_jobs()
    assert len(jobs) >= 1
    print("[PASS] Job listing verified, total jobs in DB:", len(jobs))

    # 4. Fetch Manifest from disk
    manifest = get_job_manifest(job_id)
    assert manifest.job_id == job_id
    assert manifest.status.value == "DRAFT"
    print("[PASS] On-disk manifest verified, status:", manifest.status.value)

    # 5. Fetch Logs
    logs = get_job_logs(job_id)
    assert len(logs) >= 1
    print("[PASS] Job audit logs verified, entries:", len(logs))

    print("\nAll Phase 1 foundational tests passed successfully!")

if __name__ == "__main__":
    test_phase1()
