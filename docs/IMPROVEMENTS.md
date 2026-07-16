# Improvement Map

A roadmap of potential improvements and upgrades, from direct UX fixes to longer-range
ideas. Effort estimates: S (hours), M (a day-ish), L (multi-day). Items marked
**[new renders only]** live in the JavaScript baked into each generated transcript,
so they apply to transcripts created after the change; Python-side changes benefit
existing transcripts too.

## Tier 1 — direct UX fixes

| # | Improvement | Effort | Status |
|---|---|---|---|
| 0 | Groundwork: single paragraph event-wiring path + clean save serialization | S | ✅ |
| 1 | Break up whole-turn paragraphs (pause / sentence / length heuristics) | S/M | ✅ |
| 2 | Word-level find highlighting (CSS Custom Highlight API) | M | ✅ |
| 3 | Better paragraph splitting (right-click menu, caret snapping, Ctrl+Enter) | M | ✅ |
| 4 | Descript-style speaker dropdown (assign / new / rename everywhere) | M | ✅ |
| 5 | Rename from Library + title sync between transcript, sidecar, and library | S/M | ✅ |
| 6 | Autosave with backup file + save-on-navigate + save-on-close | M | ✅ |
| 7 | Real (non-theater) progress bar during transcription | L | ✅ |

Notes:

- **1. Paragraph breaking.** One paragraph per speaker turn made long monologues unreadable.
  Paragraphs now also break within a turn at pauses ≥ 1.75s, at sentence boundaries once a
  paragraph passes ~550 characters, and unconditionally at ~900 characters. Constants are
  tunable at the top of `transcribe.py`. Continuation paragraphs show a dimmed speaker label.
  **[new renders only]**
- **2. Find.** Matches now highlight the exact word/phrase (even across word spans or inside a
  word) using the CSS Custom Highlight API — zero DOM mutation, so playback word-highlighting
  and saved files are untouched. Falls back to the old paragraph highlight on engines without
  the API. **[new renders only]**
- **3–4. Splitting & speakers.** Right-click (or Ctrl+Enter at the caret) opens a menu with
  "Split paragraph here" and direct speaker assignment; clicks between words snap to the next
  word instead of silently failing. Clicking a speaker label opens a picker listing every
  speaker in the transcript plus "New speaker…" and "Rename everywhere…". Shift+click splitting
  still works. **[new renders only]**
- **5. Rename.** Pencil icon in the Library renames a transcript (updates the sidecar metadata
  and patches the transcript file; the folder name is intentionally left alone as an opaque
  storage key). Editing the title inside a transcript now propagates to the library on save —
  this works for old transcripts too.
- **6. Autosave.** Edits (text, speakers, splits, bookmarks, title) autosave ~2.5s after you
  stop; the footer shows Unsaved → Saving → Saved. Navigating to the Library/Home flushes
  pending edits first, and closing the app flushes via pywebview's closing hook. Each save
  keeps the previous version as `transcript.html.bak` next to the transcript. **[new renders
  only, except the .bak safety net and title sync]**
- **7. Progress bar.** Weighted stages: download 15% (yt-dlp progress hooks — real), encode 10%
  (ffmpeg `-progress` vs. known duration — real), transcription 30% (real chunk boundaries;
  estimated fill within a chunk), diarization 40% (real AssemblyAI states; estimated fill),
  render 5%. Estimated portions render with a pulsing style and "~" label rather than
  pretending to be exact.

## Tier 2 — high-value next steps

- **Persist `segments.json`** ✅ — raw segments/words/diarization saved at render time.
  Unlocks future re-rendering, exact-timing exports, and re-diarization. (Old transcripts
  don't have one; that data is unrecoverable for them.)
- **Merge paragraphs** ✅ — "Merge with paragraph above" in the right-click menu; the inverse
  of split, with bookmarks surviving the merge.
- **Export SRT/VTT** ✅ — the nav's export buttons collapsed into one "↓ Export" menu
  (.txt / .md / .srt / .vtt). Subtitle cues come from the word timings in the DOM: ≤ 12 words
  or 6s per cue, early breaks at pauses; SRT carries speaker prefixes, VTT uses voice tags.
- **Speaker-count hint** ✅ — optional "Speakers" field passed to AssemblyAI's
  `speakers_expected`; the single biggest lever on diarization quality. Blank = auto.
- **Find-and-replace** ✅ — Ctrl+H (or the ⇄ button in the find bar). Replaces matches that
  sit inside a single word/text node — the misheard-name case — and skips + reports matches
  spanning multiple words, so playback timings can't be corrupted.
- **Library upgrades** ✅ — duration column plus a sort select (Newest / Oldest / Title A–Z /
  Longest).
- **Refresh old transcripts** ✅ — the ⟳ button per row (or "⟳ Update all") re-renders an
  existing transcript through the current template. Edits (text, speaker names, bookmarks,
  splits, title) are parsed out of the saved DOM and preserved; the previous file is kept as
  `transcript.html.bak`. This is what turns every future viewer improvement into "one click
  for all files" instead of "new files only".

## Tier 3 — lower ROI / bigger swings (mapped for completeness)

- **Color-coded speaker labels** (à la Descript) — easy polish after the speaker picker; low
  urgency.
- **Undo/redo for structural edits** — contenteditable already has native text undo; a
  structural undo stack (splits, renames) is real work for modest payoff now that autosave
  keeps a `.bak`.
- **Word-timestamp resync after heavy edits** — edited text drifts from karaoke word spans;
  true resync needs forced re-alignment. L effort, marginal benefit for a reading tool.
- **Local/offline transcription** (whisper.cpp / faster-whisper) — a big architectural swing;
  the cloud pipeline is the app's simplicity. Only worth it if privacy or cost becomes a
  driver.
- **Streaming/live transcription, multi-window, database-backed library** — out of character
  for a "simple" tool.

## Borrowed best practices

- **Descript**: identify-a-speaker-once-reuse-everywhere picker; moving a speaker boundary by
  pointing at a word (inspiration for the click-a-word split); color-coded labels.
  (help.descript.com — Speakers; Detect and label speakers.)
- **CSS Custom Highlight API**: supported by both engines this app ships on (WebView2/Chromium
  105+, WKWebView/Safari 17.2+). (caniuse.com/mdn-api_highlight)
- **Transcript segmentation practice**: break at speaker change first, then at long pauses and
  sentence boundaries with a length cap (~50–100 words), preferring semantically coherent break
  points. (Oral-history segmentation literature; text-based video-editing patents.)
