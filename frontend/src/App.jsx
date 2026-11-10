const { useState, useEffect, useRef } = React;

function App() {
  const [health, setHealth] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [selectedJob, setSelectedJob] = useState(null);
  const [manifest, setManifest] = useState(null);
  const [logs, setLogs] = useState([]);
  const [qcReport, setQcReport] = useState(null);
  const [referenceSheet, setReferenceSheet] = useState(null);
  const [queueStatus, setQueueStatus] = useState(null);
  const [watcher, setWatcher] = useState(null);
  const [presets, setPresets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [copiedIndex, setCopiedIndex] = useState(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [activeTab, setActiveTab] = useState("scenes"); // scenes, reference, qc

  // Form states
  const [title, setTitle] = useState("");
  const [topic, setTopic] = useState("");
  const [visualStyle, setVisualStyle] = useState("deep_dive_3d");
  const [selectedPreset, setSelectedPreset] = useState("preset_30s");
  const [qualityMode, setQualityMode] = useState("BALANCED");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  const selectedJobIdRef = useRef(null);
  const loadedReferenceJobIdRef = useRef(null);
  const isPollingRef = useRef(false);

  // Keep ref synchronized with state
  useEffect(() => {
    selectedJobIdRef.current = selectedJob?.id || null;
  }, [selectedJob?.id]);

  const selectJob = async (jobId, showLoading = true) => {
    if (!jobId) return;
    selectedJobIdRef.current = jobId;
    if (showLoading) setLoading(true);
    try {
      const [jRes, mRes, lRes] = await Promise.all([
        fetch(`http://127.0.0.1:8000/api/jobs/${jobId}`),
        fetch(`http://127.0.0.1:8000/api/jobs/${jobId}/manifest`),
        fetch(`http://127.0.0.1:8000/api/jobs/${jobId}/logs`)
      ]);

      let jobData = null;
      if (jRes.ok) {
        jobData = await jRes.json();
        setSelectedJob(jobData);
      }
      if (mRes.ok) setManifest(await mRes.json());
      if (lRes.ok) setLogs(await lRes.json());

      // Only fetch reference sheet if job has progressed past DRAFT
      // and avoid re-fetching on every 3s polling tick if already loaded
      const postDraftStatuses = ["SCRIPT_READY", "AUDIO_PROCESSING", "AUDIO_READY", "VISUALS_PENDING", "AWAITING_CLIPS", "COMPOSITING", "RENDERING", "COMPLETED"];
      if (postDraftStatuses.includes(jobData?.status)) {
        if (loadedReferenceJobIdRef.current !== jobId) {
          try {
            const refRes = await fetch(`http://127.0.0.1:8000/api/jobs/${jobId}/reference-sheet`);
            if (refRes.ok) {
              const refData = await refRes.json();
              if (refData.ready !== false && (refData.primary_character || refData.reference_sheet)) {
                setReferenceSheet(refData.reference_sheet || refData);
                loadedReferenceJobIdRef.current = jobId;
              } else {
                setReferenceSheet(null);
              }
            } else {
              setReferenceSheet(null);
            }
          } catch (_) {
            setReferenceSheet(null);
          }
        }
      } else {
        setReferenceSheet(null);
        loadedReferenceJobIdRef.current = null;
      }

      // ONLY fetch /qc if the job status is COMPLETED
      if (jobData?.status === "COMPLETED") {
        try {
          const qcRes = await fetch(`http://127.0.0.1:8000/api/jobs/${jobId}/qc`);
          if (qcRes.ok) {
            const qcData = await qcRes.json();
            setQcReport(qcData.report || (qcData.overall_status ? qcData : null));
          } else {
            setQcReport(null);
          }
        } catch (_) {
          setQcReport(null);
        }
      } else {
        setQcReport(null);
      }
    } catch (err) {
      console.error("Error loading job details:", err);
    } finally {
      if (showLoading) setLoading(false);
    }
  };

  const loadData = async (targetJobId = null) => {
    if (isPollingRef.current) return;
    isPollingRef.current = true;
    try {
      const [hRes, jRes, qRes, pRes, wRes] = await Promise.all([
        fetch("http://127.0.0.1:8000/api/health"),
        fetch("http://127.0.0.1:8000/api/jobs"),
        fetch("http://127.0.0.1:8000/api/queue/status"),
        fetch("http://127.0.0.1:8000/api/presets"),
        fetch("http://127.0.0.1:8000/api/downloads-watcher/status").catch(() => null)
      ]);

      if (hRes.ok) setHealth(await hRes.json());
      if (qRes.ok) setQueueStatus(await qRes.json());
      if (wRes && wRes.ok) setWatcher(await wRes.json());
      if (pRes.ok) {
        const pData = await pRes.json();
        setPresets(pData.presets || []);
      }

      if (jRes.ok) {
        const jData = await jRes.json();
        setJobs(jData);

        const currentId = targetJobId || selectedJobIdRef.current;
        if (currentId) {
          // Quietly refresh currently selected job details without full screen spinner
          await selectJob(currentId, false);
        } else if (jData.length > 0) {
          // Select initial job on first launch
          await selectJob(jData[0].id, false);
        }
      }
    } catch (err) {
      console.warn("Backend not yet reachable on http://127.0.0.1:8000", err);
    } finally {
      isPollingRef.current = false;
    }
  };

  // Fixed, throttled polling interval (3000ms), single timer with cleanup on unmount
  useEffect(() => {
    loadData();
    const interval = setInterval(() => {
      loadData();
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  // 1-Click: Create Project and IMMEDIATELY execute Full Video Pipeline
  const handleCreateAndGenerate = async (e) => {
    if (e) e.preventDefault();
    setFormError("");
    if (!title.trim() || !topic.trim()) {
      setFormError("Please enter both the title and story description.");
      return;
    }

    setIsSubmitting(true);
    setStatusMessage("⚡ Creating project and launching 1-Click Automated Pipeline...");
    try {
      const res = await fetch("http://127.0.0.1:8000/api/jobs/create-and-generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          topic: topic.trim(),
          visual_style: visualStyle,
          quality_mode: qualityMode
        })
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed to create & generate video");
      }

      const data = await res.json();
      const newJobId = data.job.id;
      setTitle("");
      setTopic("");
      setStatusMessage("🚀 Full Video Generation started! Script, Kokoro voice, AI visuals, and FFmpeg rendering are running automatically.");
      await selectJob(newJobId);
      await loadData(newJobId);
    } catch (err) {
      setFormError(err.message);
      setStatusMessage(`Error: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  // 1-Click for existing selected job
  const handleGenerateFullVideoForJob = async () => {
    if (!selectedJob) return;
    setActionLoading(true);
    setStatusMessage("⚡ Launching 1-Click Full Video Generation...");
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/jobs/${selectedJob.id}/generate-full-video`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          quality_mode: qualityMode
        })
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Full video generation failed");
      }
      setStatusMessage("🚀 Full Video Generation queued! Generating script, audio, visuals, and rendering final MP4...");
      await loadData(selectedJob.id);
    } catch (err) {
      setStatusMessage(`Generation Error: ${err.message}`);
    } finally {
      setActionLoading(false);
    }
  };

  const handleOpenFolder = async () => {
    if (!selectedJob) return;
    try {
      await fetch(`http://127.0.0.1:8000/api/jobs/${selectedJob.id}/open-folder`, { method: "POST" });
    } catch (err) {
      console.error("Failed to open folder:", err);
    }
  };

  const handleCancelQueue = async (jobId) => {
    try {
      await fetch(`http://127.0.0.1:8000/api/queue/cancel/${jobId}`, { method: "POST" });
      await loadData();
      if (selectedJob?.id === jobId) selectJob(jobId);
    } catch (err) {
      console.error(err);
    }
  };

  const copyPrompt = (promptText, index) => {
    navigator.clipboard.writeText(promptText);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const getStatusColor = (status) => {
    switch (status) {
      case "COMPLETED": return "bg-green-500/20 text-green-400 border-green-500/30";
      case "COMPOSITING":
      case "RENDERING":
      case "AUDIO_PROCESSING":
      case "SCRIPT_GENERATING":
      case "VIDEO_GENERATING": return "bg-blue-500/20 text-blue-400 border-blue-500/30 animate-pulse";
      case "SCENES_VALIDATED": return "bg-indigo-500/20 text-indigo-400 border-indigo-500/30";
      case "AWAITING_CLIPS":
      case "VISUALS_PENDING":
      case "AWAITING_SCENES": return "bg-amber-500/20 text-amber-400 border-amber-500/30";
      case "FAILED": return "bg-red-500/20 text-red-400 border-red-500/30";
      default: return "bg-gray-500/20 text-gray-400 border-gray-500/30";
    }
  };

  const isJobActive = queueStatus?.active_job?.job_id === selectedJob?.id;
  const isJobQueued = queueStatus?.queued_jobs?.some(q => q.job_id === selectedJob?.id);
  const isProcessing = isJobActive || isJobQueued;

  return (
    <div className="min-h-screen flex flex-col bg-[#0B0F19] text-gray-100">
      {/* Top Header */}
      <header className="border-b border-gray-800 bg-[#111827]/80 backdrop-blur sticky top-0 z-50 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center font-bold text-white shadow-lg shadow-blue-500/20">
            🎬
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-white flex items-center gap-2">
              AI Faceless Shorts Studio
              <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono font-medium">Fully Automated</span>
            </h1>
            <p className="text-xs text-gray-400">1-Click Production • Gemini/Veo Video API • Kokoro Audio • EBU R128 Subtitles • Quality Gate</p>
          </div>
        </div>

        {/* Hardware & Live Queue Status */}
        <div className="flex items-center space-x-3 text-xs font-mono">
          {watcher?.running ? (
            <div
              className="hidden md:flex items-center space-x-2 px-3 py-1.5 rounded-md bg-emerald-950/40 border border-emerald-700/50"
              title={`Watching ${watcher.downloads_dir}\nName clips scene_01.mp4, scene_02.mp4 ...${watcher.active_job_title ? `\nTarget project: ${watcher.active_job_title}` : "\nNo project is waiting for clips right now"}`}
            >
              <span className="text-emerald-300 font-semibold">🟢 Watching Downloads folder</span>
              {watcher.scenes_total > 0 && watcher.active_job_id && (
                <span className="text-emerald-400/80">{watcher.scenes_ready}/{watcher.scenes_total} clips</span>
              )}
            </div>
          ) : (
            <div className="hidden md:flex items-center space-x-2 px-3 py-1.5 rounded-md bg-gray-800/80 border border-gray-700">
              <span className="text-gray-400">⚪ Downloads watcher off</span>
            </div>
          )}
          <div className="flex items-center space-x-2 px-3 py-1.5 rounded-md bg-gray-800/80 border border-gray-700">
            <span className={`w-2 h-2 rounded-full ${queueStatus?.active_job ? 'bg-amber-400 animate-ping' : 'bg-green-400'}`}></span>
            <span className="text-gray-300">
              {queueStatus?.active_job ? `Processing Stage: ${queueStatus.active_job.current_stage}` : 'Studio Ready'}
            </span>
          </div>

          <div className="hidden md:flex items-center space-x-2 px-3 py-1.5 rounded-md bg-gray-800/80 border border-gray-700">
            <span className="text-gray-400">GPU:</span>
            <span className="text-blue-400 font-semibold">RTX 3050 (MediaFoundation)</span>
            <span className="text-gray-500">•</span>
            <span className="text-emerald-400 font-semibold">16GB RAM Protected</span>
          </div>
        </div>
      </header>

      {/* Main Studio Body */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
        
        {/* Left Side: Creation Form, Presets & Project Queue (4 cols) */}
        <div className="lg:col-span-4 space-y-6">
          
          {/* 1-Click Create & Generate Card */}
          <div className="gradient-border rounded-xl p-5 shadow-xl">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4 flex items-center gap-2">
              <span>⚡</span> 1-Click Video Generator
            </h2>

            {formError && (
              <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
                {formError}
              </div>
            )}

            <form onSubmit={handleCreateAndGenerate} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1.5">Short Title</label>
                <input
                  type="text"
                  placeholder="e.g. The Secrets of Deep Sea Bioluminescence"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="w-full px-3.5 py-2.5 rounded-lg bg-gray-900/80 border border-gray-700 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition"
                  disabled={isSubmitting}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1.5">Story / Narrative Hook (Up to 10,000 chars)</label>
                <textarea
                  placeholder="Enter your complete story description, viral hook, or script concept..."
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  rows="4"
                  className="w-full px-3.5 py-2.5 rounded-lg bg-gray-900/80 border border-gray-700 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition"
                  disabled={isSubmitting}
                />
              </div>

              {/* Production Presets */}
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1.5">Target Duration & Pacing</label>
                <select
                  value={selectedPreset}
                  onChange={(e) => setSelectedPreset(e.target.value)}
                  className="w-full px-3.5 py-2.5 rounded-lg bg-gray-900/80 border border-gray-700 text-sm text-gray-100 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition"
                >
                  <option value="preset_30s">⚡ 30s Viral Hook (~75 words)</option>
                  <option value="preset_45s">📖 45s Story Arc (~110 words)</option>
                  <option value="preset_60s">🔬 60s Deep Dive (~150 words)</option>
                  <option value="preset_draft">🚀 Fast Draft Preview</option>
                </select>
              </div>

              {/* Visual Style */}
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1.5">Visual Aesthetic</label>
                <select
                  value={visualStyle}
                  onChange={(e) => setVisualStyle(e.target.value)}
                  className="w-full px-3.5 py-2.5 rounded-lg bg-gray-900/80 border border-gray-700 text-sm text-gray-100 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition"
                  disabled={isSubmitting}
                >
                  <option value="deep_dive_3d">🌊 Deep Dive 3D (Unreal 5 Nanite Macro)</option>
                  <option value="tall_tale_3d">🧸 Tall Tale 3D (Tactile Claymation)</option>
                  <option value="inside_out">🔍 Inside Out (Mechanical/Anatomy Cutaway)</option>
                  <option value="stick_story">⚔️ Stick Story (3D Porcelain Physics)</option>
                  <option value="night_file_3d">🌲 Night File 3D (Found-Footage VHS Mystery)</option>
                  <option value="paper_atlas">📜 Paper Atlas (Layered Pop-Up Papercraft)</option>
                </select>
              </div>

              {/* Render Quality Mode */}
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1.5">Encoding Mode</label>
                <div className="grid grid-cols-3 gap-2">
                  {["DRAFT", "BALANCED", "HIGH_QUALITY"].map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setQualityMode(mode)}
                      className={`py-1.5 text-xs rounded-lg border font-medium transition ${
                        qualityMode === mode
                          ? 'bg-blue-600/30 border-blue-500 text-blue-300'
                          : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
                      }`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
              </div>

              {/* HERO 1-CLICK BUTTON */}
              <button
                type="submit"
                disabled={isSubmitting}
                className="w-full mt-2 py-3 px-4 rounded-xl bg-gradient-to-r from-emerald-600 via-teal-600 to-blue-600 hover:from-emerald-500 hover:to-blue-500 text-white font-bold text-sm shadow-xl shadow-emerald-600/25 disabled:opacity-50 transition flex items-center justify-center gap-2"
              >
                <span>⚡</span>
                {isSubmitting ? "Creating & Generating..." : "⚡ Create & Generate New Short"}
              </button>
            </form>
          </div>

          {/* Sequential Queue Live Status Card */}
          <div className="gradient-border rounded-xl p-5 shadow-xl">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 flex items-center gap-2">
                <span>🔄</span> Automated Production Queue
              </h2>
              <span className="text-xs px-2 py-0.5 rounded-full bg-gray-800 text-gray-400 font-mono">
                {queueStatus?.queued_count || 0} waiting
              </span>
            </div>

            {queueStatus?.active_job ? (
              <div className="p-3.5 rounded-lg bg-blue-950/40 border border-blue-800/50 mb-3">
                <div className="flex items-center justify-between text-xs mb-1">
                  <span className="font-semibold text-blue-300 flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-full bg-blue-400 animate-ping"></span>
                    Running: {queueStatus.active_job.job_id.slice(0, 8)}...
                  </span>
                  <span className="text-blue-400 font-mono font-semibold">{queueStatus.active_job.current_stage}</span>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-1.5 mt-2 overflow-hidden">
                  <div
                    className="bg-gradient-to-r from-blue-500 to-emerald-400 h-1.5 rounded-full transition-all duration-300"
                    style={{ width: `${queueStatus.active_job.progress_percent}%` }}
                  ></div>
                </div>
                <div className="mt-2 flex justify-between items-center text-[11px]">
                  <span className="text-gray-400">{queueStatus.active_job.action}</span>
                  <button
                    onClick={() => handleCancelQueue(queueStatus.active_job.job_id)}
                    className="text-red-400 hover:text-red-300 font-medium"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-xs text-gray-500 italic mb-3">Queue is idle. Jobs execute 1-at-a-time to protect system RAM.</p>
            )}

            {/* Waiting Queue List */}
            {queueStatus?.queued_jobs?.length > 0 && (
              <div className="space-y-2">
                <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">Up Next in Queue:</p>
                {queueStatus.queued_jobs.map((item, idx) => (
                  <div key={item.job_id} className="flex items-center justify-between p-2 rounded bg-gray-900/60 border border-gray-800 text-xs">
                    <span className="font-mono text-gray-300">#{idx + 1} {item.job_id.slice(0, 8)} ({item.action})</span>
                    <button
                      onClick={() => handleCancelQueue(item.job_id)}
                      className="text-gray-500 hover:text-red-400"
                      title="Cancel from queue"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Job History / Project Switcher */}
          <div className="gradient-border rounded-xl p-5 shadow-xl">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4 flex items-center justify-between">
              <span>📁 Projects ({jobs.length})</span>
            </h2>

            <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
              {jobs.length === 0 ? (
                <p className="text-xs text-gray-500 italic text-center py-4">No jobs created yet</p>
              ) : (
                jobs.map((j) => (
                  <div
                    key={j.id}
                    onClick={() => selectJob(j.id)}
                    className={`p-3 rounded-lg border text-left cursor-pointer transition ${
                      selectedJob?.id === j.id
                        ? "bg-blue-600/10 border-blue-500/50 shadow-md shadow-blue-500/5"
                        : "bg-gray-900/40 border-gray-800/80 hover:bg-gray-800/50 hover:border-gray-700"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-xs font-semibold text-gray-200 line-clamp-1">{j.title}</p>
                      <span className={`text-[10px] px-2 py-0.5 rounded-full border font-mono ${getStatusColor(j.status)}`}>
                        {j.status}
                      </span>
                    </div>
                    <p className="text-[11px] text-gray-500 mt-1 line-clamp-1">{j.topic}</p>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        {/* Right Side: Production Workspace (8 cols) */}
        <div className="lg:col-span-8 space-y-6">
          
          {selectedJob ? (
            <div className="space-y-6">
              
              {/* Job Header & Action Bar */}
              <div className="gradient-border rounded-xl p-6 shadow-xl">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-gray-800">
                  <div>
                    <div className="flex items-center gap-3">
                      <h2 className="text-xl font-bold text-white tracking-tight">{selectedJob.title}</h2>
                      <span className={`text-xs px-2.5 py-0.5 rounded-full border font-mono font-medium ${getStatusColor(selectedJob.status)}`}>
                        {selectedJob.status}
                      </span>
                    </div>
                    <p className="text-xs text-gray-400 mt-1">
                      Job ID: <span className="font-mono text-gray-300">{selectedJob.id}</span> • Style: <span className="text-blue-400 font-medium capitalize">{selectedJob.visual_style?.replace('_', ' ')}</span>
                    </p>
                  </div>

                  {/* Actions for current job */}
                  <div className="flex flex-wrap items-center gap-2">
                    {selectedJob.status !== "COMPLETED" && (
                      <button
                        onClick={handleGenerateFullVideoForJob}
                        disabled={actionLoading || isProcessing}
                        className={`px-4 py-2 rounded-lg text-white text-xs font-bold shadow-lg transition flex items-center gap-1.5 ${
                          isProcessing
                            ? "bg-blue-900/60 border border-blue-500/40 text-blue-300 cursor-not-allowed opacity-80"
                            : "bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 shadow-blue-600/25 disabled:opacity-50"
                        }`}
                      >
                        {isProcessing ? (
                          <>
                            <span className="w-2 h-2 rounded-full bg-blue-400 animate-ping"></span>
                            <span>Processing in queue...</span>
                          </>
                        ) : (
                          <>
                            <span>⚡</span>
                            <span>{actionLoading ? "Queueing..." : "⚡ Resume / Re-run This Short"}</span>
                          </>
                        )}
                      </button>
                    )}

                    {selectedJob.status === "COMPLETED" && (
                      <>
                        <a
                          href={`http://127.0.0.1:8000/api/jobs/${selectedJob.id}/video?download=true`}
                          className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold shadow-lg shadow-emerald-600/20 transition flex items-center gap-1.5"
                        >
                          <span>⬇️</span> Download MP4
                        </a>
                        <button
                          onClick={handleOpenFolder}
                          className="px-3.5 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs font-medium border border-gray-700 transition flex items-center gap-1.5"
                        >
                          <span>📁</span> Open Folder
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {/* Status Alert Banner */}
                {statusMessage && (
                  <div className="mt-4 p-3 rounded-lg bg-blue-500/10 border border-blue-500/30 text-blue-300 text-xs flex items-center justify-between">
                    <span>{statusMessage}</span>
                    <button onClick={() => setStatusMessage("")} className="text-blue-400 hover:text-white">✕</button>
                  </div>
                )}

                {/* Active Job Real-Time Stage Tracker */}
                {isJobActive && (
                  <div className="mt-4 p-4 rounded-xl bg-blue-950/30 border border-blue-500/40 space-y-2">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-semibold text-blue-300 flex items-center gap-2">
                        <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
                        Full Automated Pipeline in Progress: {queueStatus.active_job.current_stage}
                      </span>
                      <span className="font-mono text-emerald-400 font-bold">{queueStatus.active_job.progress_percent}%</span>
                    </div>
                    <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
                      <div
                        className="bg-gradient-to-r from-blue-500 to-emerald-400 h-2 rounded-full transition-all duration-300"
                        style={{ width: `${queueStatus.active_job.progress_percent}%` }}
                      ></div>
                    </div>
                    <div className="flex items-center justify-between text-[11px] text-gray-400 pt-1">
                      <span>1. Script & Prompts</span>
                      <span>2. Kokoro Voice</span>
                      <span>3. ASS Subtitles</span>
                      <span>4. Gemini Video API</span>
                      <span>5. FFmpeg Render & QC</span>
                    </div>
                  </div>
                )}

                {/* Sub-Navigation Tabs */}
                <div className="flex items-center gap-3 mt-4 text-xs font-medium border-b border-gray-800 pb-2">
                  <button
                    onClick={() => setActiveTab("scenes")}
                    className={`pb-1 transition ${activeTab === 'scenes' ? 'text-blue-400 border-b-2 border-blue-400 font-semibold' : 'text-gray-400 hover:text-gray-200'}`}
                  >
                    🎬 Story Scenes & AI Footage ({manifest?.scenes?.length || 0})
                  </button>
                  <button
                    onClick={() => setActiveTab("reference")}
                    className={`pb-1 transition ${activeTab === 'reference' ? 'text-blue-400 border-b-2 border-blue-400 font-semibold' : 'text-gray-400 hover:text-gray-200'}`}
                  >
                    👤 Visual Continuity Sheet
                  </button>
                  <button
                    onClick={() => setActiveTab("qc")}
                    className={`pb-1 transition ${activeTab === 'qc' ? 'text-blue-400 border-b-2 border-blue-400 font-semibold' : 'text-gray-400 hover:text-gray-200'}`}
                  >
                    🛡️ Quality Control {qcReport ? `(${qcReport.overall_status})` : ''}
                  </button>
                </div>
              </div>

              {/* RENDERED FINAL VIDEO PLAYER (If completed) */}
              {selectedJob.status === "COMPLETED" && (
                <div className="gradient-border rounded-xl p-6 shadow-xl space-y-4">
                  <div className="flex items-center justify-between border-b border-gray-800 pb-3">
                    <div>
                      <h3 className="text-base font-bold text-white flex items-center gap-2">
                        <span>🎬</span> Finished 9:16 Short Ready!
                      </h3>
                      <p className="text-xs text-emerald-400">Audio normalized (-14 LUFS) • Subtitles burned • 1080x1920 MP4</p>
                    </div>

                    <div className="flex items-center gap-2">
                      <a
                        href={`http://127.0.0.1:8000/api/jobs/${selectedJob.id}/video?download=true`}
                        className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-bold shadow-md shadow-emerald-600/30 flex items-center gap-1.5 transition"
                      >
                        <span>⬇️</span> Download Finished MP4
                      </a>
                      <button
                        onClick={handleOpenFolder}
                        className="text-xs px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 font-medium border border-gray-700 transition"
                      >
                        📁 Open Folder
                      </button>
                    </div>
                  </div>

                  <div className="flex justify-center bg-black/80 rounded-xl p-4">
                    <video
                      controls
                      autoPlay={false}
                      className="rounded-lg shadow-2xl max-h-[500px] w-auto aspect-[9/16] object-cover border border-gray-800"
                      src={`http://127.0.0.1:8000/api/jobs/${selectedJob.id}/video`}
                    >
                      Your browser does not support HTML5 video.
                    </video>
                  </div>
                </div>
              )}

              {/* TAB 1: SCENES & AI FOOTAGE */}
              {activeTab === "scenes" && (
                <div className="space-y-4">
                  {manifest?.scenes?.length > 0 ? (
                    manifest.scenes.map((scene, idx) => (
                      <div key={idx} className="gradient-border rounded-xl p-5 shadow-lg space-y-3">
                        <div className="flex items-center justify-between border-b border-gray-800/80 pb-3">
                          <div className="flex items-center space-x-2">
                            <span className="px-2 py-0.5 rounded bg-blue-500/20 text-blue-400 font-mono text-xs font-semibold">
                              Scene {scene.index + 1}
                            </span>
                            <span className="text-xs text-gray-400 font-mono">
                              ~{scene.duration_seconds?.toFixed(1)}s
                            </span>
                          </div>

                          {/* AI Video Generation Status */}
                          <div>
                            {scene.clip_file ? (
                              <span className="flex items-center space-x-1 px-2.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 text-xs font-medium">
                                <span>✓</span>
                                <span>AI Footage Generated: {scene.clip_file.split('\\').pop().split('/').pop()}</span>
                              </span>
                            ) : isJobActive ? (
                              <span className="flex items-center space-x-1.5 px-2.5 py-0.5 rounded-full bg-blue-500/20 text-blue-400 border border-blue-500/30 text-xs font-medium animate-pulse">
                                <span className="w-2 h-2 rounded-full bg-blue-400"></span>
                                <span>Generating via Gemini Video API...</span>
                              </span>
                            ) : watcher?.running && watcher.active_job_id === selectedJob.id ? (
                              <span className="flex items-center space-x-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-300 border border-emerald-500/30 text-xs font-medium animate-pulse">
                                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
                                <span>Waiting for scene_{String(scene.index + 1).padStart(2, '0')}.mp4 in Downloads...</span>
                              </span>
                            ) : (
                              <span className="flex items-center space-x-1 px-2.5 py-0.5 rounded-full bg-gray-500/20 text-gray-400 border border-gray-500/30 text-xs font-medium">
                                <span>⏳</span>
                                <span>Pending Generation</span>
                              </span>
                            )}
                          </div>
                        </div>

                        {/* Narration */}
                        <div>
                          <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-400 mb-1">Narration</p>
                          <p className="text-xs text-gray-200 italic bg-gray-900/60 p-2.5 rounded-lg border border-gray-800">
                            "{scene.narration}"
                          </p>
                        </div>

                        {/* Visual Prompt */}
                        <div>
                          <div className="flex items-center justify-between mb-1">
                            <p className="text-[11px] font-semibold uppercase tracking-wider text-blue-400">
                              Visual Prompt (Sent to Gemini/Veo API)
                            </p>
                            <button
                              onClick={() => copyPrompt(scene.visual_prompt, idx)}
                              className="text-xs text-blue-400 hover:text-blue-300 font-medium flex items-center gap-1"
                            >
                              {copiedIndex === idx ? "✓ Copied!" : "📋 Copy"}
                            </button>
                          </div>
                          <pre className="text-xs font-mono text-gray-300 bg-black/40 p-3 rounded-lg border border-gray-800/80 whitespace-pre-wrap select-all">
                            {scene.visual_prompt}
                          </pre>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="text-center py-12 rounded-xl border border-dashed border-gray-800">
                      <p className="text-sm text-gray-400 mb-3">No scenes generated yet.</p>
                      {selectedJob.status !== "COMPLETED" && (
                        <button
                          onClick={handleGenerateFullVideoForJob}
                          disabled={actionLoading || isProcessing}
                          className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-bold shadow-lg shadow-blue-600/20 disabled:opacity-50"
                        >
                          {isProcessing ? "Processing in queue..." : "⚡ Resume / Re-run This Short"}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* TAB 2: VISUAL REFERENCE & CONTINUITY */}
              {activeTab === "reference" && (
                <div className="gradient-border rounded-xl p-6 shadow-xl space-y-6">
                  <div className="flex items-center justify-between border-b border-gray-800 pb-3">
                    <h3 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
                      <span>🎨</span> Project Visual Continuity Specification
                    </h3>
                    <span className="text-xs text-gray-400 font-mono">Character & World Consistency</span>
                  </div>

                  {referenceSheet ? (
                    <div className="space-y-5 text-xs">
                      {/* Character Spec */}
                      <div className="p-4 rounded-lg bg-gray-900/60 border border-gray-800 space-y-2">
                        <h4 className="font-semibold text-blue-300 flex items-center gap-1.5 uppercase tracking-wider">
                          <span>👤</span> Primary Character Specification
                        </h4>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2 text-gray-300">
                          <div><span className="text-gray-500">Name:</span> {referenceSheet.primary_character?.name || 'N/A'}</div>
                          <div><span className="text-gray-500">Role:</span> {referenceSheet.primary_character?.role || 'N/A'}</div>
                          <div className="md:col-span-2"><span className="text-gray-500">Appearance:</span> {referenceSheet.primary_character?.appearance}</div>
                          <div className="md:col-span-2"><span className="text-gray-500">Clothing & Materials:</span> {referenceSheet.primary_character?.clothing}</div>
                          <div className="md:col-span-2"><span className="text-gray-500">Distinctive Feature:</span> {referenceSheet.primary_character?.distinctive_features}</div>
                        </div>
                      </div>

                      {/* Environment Spec */}
                      <div className="p-4 rounded-lg bg-gray-900/60 border border-gray-800 space-y-2">
                        <h4 className="font-semibold text-indigo-300 flex items-center gap-1.5 uppercase tracking-wider">
                          <span>🌐</span> World & Lighting Environment
                        </h4>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2 text-gray-300">
                          <div><span className="text-gray-500">Setting:</span> {referenceSheet.environment?.setting_name}</div>
                          <div><span className="text-gray-500">Lighting:</span> {referenceSheet.environment?.lighting_conditions}</div>
                          <div className="md:col-span-2"><span className="text-gray-500">Atmosphere:</span> {referenceSheet.environment?.atmosphere}</div>
                          <div className="md:col-span-2 flex items-center gap-2">
                            <span className="text-gray-500">Color Palette:</span>
                            {referenceSheet.environment?.color_palette?.map((c, i) => (
                              <span key={i} className="flex items-center gap-1 px-2 py-0.5 rounded bg-gray-800 border border-gray-700 font-mono text-[11px]">
                                <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ backgroundColor: c }}></span>
                                {c}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="text-center py-10 rounded-xl border border-dashed border-gray-800 space-y-2">
                      <span className="text-2xl block">🎨</span>
                      <p className="text-xs text-blue-400 font-semibold">Reference sheet generating...</p>
                      <p className="text-[11px] text-gray-500 italic">Character and environmental continuity sheet generates automatically once the script stage completes.</p>
                    </div>
                  )}
                </div>
              )}

              {/* TAB 3: QUALITY CONTROL */}
              {activeTab === "qc" && (
                <div className="gradient-border rounded-xl p-6 shadow-xl space-y-6">
                  <div className="flex items-center justify-between border-b border-gray-800 pb-3">
                    <div>
                      <h3 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
                        <span>🛡️</span> Automated Quality Control Gate
                      </h3>
                      <p className="text-xs text-gray-400 mt-0.5">Automated video stream integrity, loudness & sync verification</p>
                    </div>
                  </div>

                  {qcReport ? (
                    <div className="space-y-4">
                      <div className={`p-4 rounded-xl border flex items-center justify-between ${
                        qcReport.is_production_ready
                          ? 'bg-emerald-950/30 border-emerald-500/40 text-emerald-300'
                          : 'bg-red-950/30 border-red-500/40 text-red-300'
                      }`}>
                        <div className="flex items-center gap-3">
                          <span className="text-2xl">{qcReport.is_production_ready ? '✅' : '⚠️'}</span>
                          <div>
                            <p className="font-bold text-sm">
                              {qcReport.is_production_ready ? 'PRODUCTION READY' : 'QUALITY GATE FLAGGED'}
                            </p>
                            <p className="text-xs opacity-80">
                              {qcReport.passed_checks} passed • {qcReport.failed_checks} failed • {qcReport.warning_checks} warnings
                            </p>
                          </div>
                        </div>
                        <span className={`px-3 py-1 rounded-full text-xs font-mono font-bold border ${
                          qcReport.overall_status === 'PASS' ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-400' : 'bg-red-500/20 border-red-500/50 text-red-400'
                        }`}>
                          {qcReport.overall_status}
                        </span>
                      </div>

                      <div className="space-y-2">
                        {qcReport.checks?.map((chk, idx) => (
                          <div key={idx} className="flex items-start justify-between p-3 rounded-lg bg-gray-900/60 border border-gray-800 text-xs">
                            <div className="space-y-0.5">
                              <p className="font-semibold text-gray-200">{chk.name}</p>
                              <p className="text-gray-400 text-[11px]">{chk.message}</p>
                            </div>
                            <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                              chk.status === 'PASS' ? 'bg-emerald-500/20 text-emerald-400' :
                              chk.status === 'WARN' ? 'bg-amber-500/20 text-amber-400' : 'bg-red-500/20 text-red-400'
                            }`}>
                              {chk.status}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="text-center py-10 rounded-xl border border-dashed border-gray-800 space-y-2">
                      <span className="text-2xl block">⏳</span>
                      <p className="text-xs text-blue-400 font-semibold">QC in progress...</p>
                      <p className="text-[11px] text-gray-500 italic">Quality checks run automatically when video rendering completes.</p>
                    </div>
                  )}
                </div>
              )}

            </div>
          ) : (
            <div className="gradient-border rounded-xl p-12 text-center shadow-xl">
              <span className="text-4xl mb-3 block">🎬</span>
              <p className="text-gray-400 text-sm">Enter your story on the left and click <strong>Create & Generate New Short</strong> to produce your YouTube Short!</p>
            </div>
          )}

        </div>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
