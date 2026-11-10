const API_BASE = "http://127.0.0.1:8000/api";

export async function fetchHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    if (!res.ok) throw new Error("Health check failed");
    return await res.json();
  } catch (err) {
    console.error("Health check error:", err);
    return null;
  }
}

export async function fetchJobs() {
  try {
    const res = await fetch(`${API_BASE}/jobs`);
    if (!res.ok) throw new Error("Failed to fetch jobs");
    return await res.json();
  } catch (err) {
    console.error("Fetch jobs error:", err);
    return [];
  }
}

export async function fetchJobById(jobId) {
  const res = await fetch(`${API_BASE}/jobs/${jobId}`);
  if (!res.ok) throw new Error(`Failed to fetch job ${jobId}`);
  return await res.json();
}

export async function fetchJobManifest(jobId) {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/manifest`);
  if (!res.ok) throw new Error(`Failed to fetch manifest for ${jobId}`);
  return await res.json();
}

export async function fetchJobLogs(jobId) {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/logs`);
  if (!res.ok) throw new Error(`Failed to fetch logs for ${jobId}`);
  return await res.json();
}

export async function createJob(title, topic, visualStyle) {
  const res = await fetch(`${API_BASE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      topic,
      visual_style: visualStyle,
    }),
  });
  if (!res.ok) {
    const errorData = await res.json();
    throw new Error(errorData.detail || "Failed to create job");
  }
  return await res.json();
}

export async function createDirectShort(payload) {
  const res = await fetch(`${API_BASE}/short/create_direct`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const errorData = await res.json();
    throw new Error(errorData.detail || "Failed to create direct short");
  }
  return await res.json();
}
