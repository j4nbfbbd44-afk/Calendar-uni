"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const START_H = 8, END_H = 20;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
  };

  let index = null, group = null, weekKeys = [], weekPos = 0;

  // "Now" in the timetable's own zone (UTC+5), independent of the viewer's zone.
  // Returned Date is shifted so its UTC getters read local timetable time.
  const nowTT = () => new Date(Date.now() + index.utc_offset_minutes * 60000);
  const isoDate = (d) => d.toISOString().slice(0, 10);
  const mondayOf = (d) => { const x = new Date(d); x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7)); return isoDate(x); };
  const addDays = (iso, n) => { const x = new Date(iso + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return isoDate(x); };
  const mins = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
  const fmtDate = (iso, opts) => new Date(iso + "T00:00:00Z").toLocaleDateString(undefined, { timeZone: "UTC", ...opts });
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const kindTag = (k) => k === "lecture" ? '<span class="tag">lecture</span>' : k === "seminar" ? '<span class="tag">seminar</span>' : "";

  function showError(msg) { const e = $("error"); e.textContent = msg; e.hidden = false; }

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  }

  async function init() {
    try { index = await getJSON("data/index.json"); }
    catch (e) { showError("Could not load timetable data. " + e.message); return; }

    $("source").href = index.source;
    $("updated").textContent = "Last checked: " + new Date(index.generated).toLocaleString();

    const sel = $("group");
    sel.innerHTML = "";
    const byProgram = new Map();
    for (const g of index.groups) {
      if (!byProgram.has(g.program)) byProgram.set(g.program, []);
      byProgram.get(g.program).push(g);
    }
    const ph = document.createElement("option");
    ph.value = ""; ph.textContent = "Choose your group…";
    sel.append(ph);
    for (const [prog, gs] of byProgram) {
      const og = document.createElement("optgroup");
      og.label = prog || "Other";
      for (const g of gs) {
        const o = document.createElement("option");
        o.value = g.slug; o.textContent = g.name;
        og.append(o);
      }
      sel.append(og);
    }

    const fromHash = new URLSearchParams(location.hash.slice(1)).get("g");
    const initial = [fromHash, store.get("group")].find((s) => s && index.groups.some((g) => g.slug === s));
    sel.addEventListener("change", () => selectGroup(sel.value));
    $("prev").addEventListener("click", () => { weekPos = Math.max(0, weekPos - 1); renderWeek(); });
    $("next").addEventListener("click", () => { weekPos = Math.min(weekKeys.length - 1, weekPos + 1); renderWeek(); });
    $("thisweek").addEventListener("click", () => { weekPos = currentWeekPos(); renderWeek(); });
    $("copy").addEventListener("click", async () => {
      const inp = $("feedurl");
      try { await navigator.clipboard.writeText(inp.value); } catch { inp.select(); document.execCommand("copy"); }
      $("copy").textContent = "Copied"; setTimeout(() => ($("copy").textContent = "Copy URL"), 1500);
    });

    if (initial) { sel.value = initial; await selectGroup(initial); }
    else renderEmpty();

    setInterval(() => { if (group) { renderToday(); renderWeek(); } }, 60000);
  }

  function renderEmpty() {
    $("today").innerHTML = '<h2>Pick your group</h2><p class="muted">Choose your group at the top to see today, the week, and calendar links.</p>';
    $("week").innerHTML = ""; $("weeklabel").textContent = ""; $("weeknote").textContent = "";
    $("subscribe").hidden = true;
  }

  async function selectGroup(slug) {
    if (!slug) { group = null; renderEmpty(); return; }
    try { group = await getJSON("data/" + slug + ".json"); }
    catch (e) { showError("Could not load this group. " + e.message); return; }
    store.set("group", slug);
    history.replaceState(null, "", "#g=" + encodeURIComponent(slug));
    document.title = group.name + " · Timetable";
    weekKeys = Object.keys(group.weeks).sort();
    weekPos = currentWeekPos();
    renderToday(); renderWeek(); renderSubscribe(); renderChanges();
  }

  function currentWeekPos() {
    const mon = mondayOf(nowTT());
    let pos = weekKeys.indexOf(mon);
    if (pos < 0) { // nearest week with data
      pos = weekKeys.findIndex((k) => k > mon);
      if (pos < 0) pos = weekKeys.length - 1;
    }
    return pos;
  }

  function eventsOn(iso) {
    const wk = group.weeks[mondayOf(new Date(iso + "T00:00:00Z"))];
    return wk ? wk.events.filter((e) => e.date === iso).sort((a, b) => mins(a.start) - mins(b.start)) : null;
  }

  function renderToday() {
    const now = nowTT();
    const today = isoDate(now);
    const nowMin = now.getUTCHours() * 60 + now.getUTCMinutes();
    const evs = eventsOn(today);
    const wkToday = group.weeks[mondayOf(now)];
    const head = `<div class="today-head"><h2>Today · ${esc(fmtDate(today, { weekday: "long", day: "numeric", month: "long" }))}</h2><span class="muted">${esc(group.name)}</span></div>` +
      (wkToday && wkToday.projected ? '<p class="muted proj">Projected from the weekly timetable — this week\'s sheet hasn\'t been fetched yet.</p>' : "");

    if (evs === null) {
      $("today").innerHTML = head + '<p class="muted">No timetable published for this week yet.</p>';
      return;
    }
    const current = evs.find((e) => mins(e.start) <= nowMin && nowMin < mins(e.end));
    const next = evs.find((e) => mins(e.start) > nowMin);
    let hero = "";
    const heroFor = (label, e) => `<div class="hero"><div class="label">${label} · ${e.start}–${e.end}</div>
      <div class="room">${esc(e.location || "Room not listed")}</div><div class="what">${esc(e.text)}</div></div>`;
    if (current) hero = heroFor("Now", current);
    else if (next) hero = heroFor(`Next in ${fmtIn(mins(next.start) - nowMin)}`, next);
    else if (evs.length) hero = '<p class="muted">No more classes today.</p>';
    else {
      const nd = nextClassDay(today);
      hero = '<p class="muted">No classes today.' + (nd ? ` Next: ${esc(fmtDate(nd.date, { weekday: "long" }))} ${nd.start} · ${esc(nd.location || nd.text)}` : "") + "</p>";
    }
    const list = evs.length ? '<ul class="daylist">' + evs.map((e) =>
      `<li class="${mins(e.end) <= nowMin ? "past" : ""}"><span class="t">${e.start}–${e.end}</span>
       <span><span class="loc">${esc(e.location || "—")}</span> · ${esc(e.text)}${kindTag(e.kind)}${sharedNote(e)}</span></li>`).join("") + "</ul>" : "";
    $("today").innerHTML = head + hero + list;
  }

  function nextClassDay(fromIso) {
    for (let i = 1; i <= 14; i++) {
      const d = addDays(fromIso, i), evs = eventsOn(d);
      if (evs && evs.length) return evs[0];
    }
    return null;
  }

  const fmtIn = (m) => m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim();
  const sharedNote = (e) => e.shared_with && e.shared_with.length ? ` <span class="tag" title="${esc(e.shared_with.join(", "))}">+${e.shared_with.length} groups</span>` : "";

  function renderWeek() {
    if (!group) return;
    const key = weekKeys[weekPos];
    const wk = group.weeks[key];
    $("prev").disabled = weekPos <= 0;
    $("next").disabled = weekPos >= weekKeys.length - 1;
    $("weeklabel").textContent = `${fmtDate(key, { day: "numeric", month: "short" })} – ${fmtDate(addDays(key, 5), { day: "numeric", month: "short", year: "numeric" })}`;
    const curMon = mondayOf(nowTT());
    $("weeknote").textContent = (key === curMon ? "This week" : key < curMon ? "Past week" : "Upcoming week") +
      (wk.projected ? " · projected from the current weekly timetable" : "");
    $("notes").innerHTML = (wk.notes || []).map((n) => `<div class="note">Also this week (no fixed time): ${esc(n)}</div>`).join("");

    const now = nowTT(), today = isoDate(now), nowMin = now.getUTCHours() * 60 + now.getUTCMinutes();
    const days = [0, 1, 2, 3, 4, 5].map((i) => addDays(key, i));
    const hourPx = 46, top = (m) => ((m - START_H * 60) / 60) * hourPx;
    const height = (END_H - START_H) * hourPx;

    let html = '<div class="dh"></div>' + days.map((d, i) =>
      `<div class="dh ${d === today ? "is-today" : ""}">${DAY_NAMES[i]}<small>${esc(fmtDate(d, { day: "numeric", month: "short" }))}</small></div>`).join("");
    html += `<div class="axis" style="height:${height}px">` +
      Array.from({ length: END_H - START_H + 1 }, (_, i) => `<span style="top:${i * hourPx}px">${START_H + i}:00</span>`).join("") + "</div>";
    for (const d of days) {
      const evs = wk.events.filter((e) => e.date === d);
      html += `<div class="col ${d === today ? "is-today" : ""}" style="height:${height}px">`;
      for (const e of evs) {
        const t = top(mins(e.start)), h = Math.max(top(mins(e.end)) - t - 2, 18);
        html += `<div class="ev ${e.kind}${wk.projected ? " projected" : ""}" style="top:${t + 1}px;height:${h}px" title="${esc(e.text + (e.shared_with.length ? "\nShared with: " + e.shared_with.join(", ") : ""))}">
          <b>${esc(e.location || "—")}</b><span class="tm">${e.start}–${e.end}</span><br>${esc(e.text)}</div>`;
      }
      if (d === today && nowMin >= START_H * 60 && nowMin <= END_H * 60) html += `<div class="nowline" style="top:${top(nowMin)}px"></div>`;
      html += "</div>";
    }
    $("week").innerHTML = html;

    // Mobile list
    let list = $("week-list");
    if (!list) { list = document.createElement("div"); list.id = "week-list"; list.className = "week-list"; $("week").after(list); }
    list.innerHTML = days.map((d, i) => {
      const evs = wk.events.filter((e) => e.date === d).sort((a, b) => mins(a.start) - mins(b.start));
      return `<h3 class="${d === today ? "is-today" : ""}">${DAY_NAMES[i]} · ${esc(fmtDate(d, { day: "numeric", month: "short" }))}</h3>` +
        (evs.length ? '<ul class="daylist">' + evs.map((e) => `<li><span class="t">${e.start}–${e.end}</span><span><span class="loc">${esc(e.location || "—")}</span> · ${esc(e.text)}${kindTag(e.kind)}</span></li>`).join("") + "</ul>"
          : '<div class="empty">No classes</div>');
    }).join("");
  }

  function renderChanges() {
    const cs = (group.changes || []).slice().reverse();
    $("changes").hidden = cs.length === 0;
    const label = { added: "Added", removed: "Removed", changed: "Changed", group_added: "New group", group_removed: "Group removed" };
    $("changelist").innerHTML = cs.map((c) =>
      `<li><span class="when">${esc(new Date(c.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }))}</span>
       <span class="kind ${esc(c.kind)}">${esc(label[c.kind] || c.kind)}</span> ${esc(c.text.replace("  ->  ", " → "))}
       <span class="muted">(week of ${esc(fmtDate(c.week, { day: "numeric", month: "short" }))})</span></li>`).join("");
  }

  function renderSubscribe() {
    $("subscribe").hidden = false;
    const url = new URL("ics/" + group.slug + ".ics", location.href);
    url.hash = ""; url.search = "";
    const https = url.href;
    const webcal = https.replace(/^https?:/, "webcal:");
    $("feedurl").value = https;
    $("sub-google").href = "https://calendar.google.com/calendar/r?cid=" + encodeURIComponent(webcal);
    $("sub-webcal").href = webcal;
    $("sub-download").href = https;
    $("sub-download").setAttribute("download", group.slug + ".ics");
  }

  init();
})();
