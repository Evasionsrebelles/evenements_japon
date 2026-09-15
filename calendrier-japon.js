(function(){
  "use strict";

  // ---- CONFIGURATION ----------------------------------------------------
  // Remplacez cette URL par le chemin vers votre propre calendrier-japon.json
  // si vous l'hébergez sur le même site (évite toute dépendance à GitHub).
  // Supporte nativement les deux formats produits par le scraper :
  //  - payload.months (kanpai.fr, événements {text, links, type, ...})
  //  - payload.japantravel_events (API JapanTravel, événements {title, event_date,
  //    event_general_price, event_free, category, url, ...}), présent tant que le
  //    scraper n'est PAS lancé avec --jt-merge.
  var DATA_URL = "https://raw.githubusercontent.com/Evasionsrebelles/evenements_japon/main/calendrier-japon.json";

  var WEEKDAY_LABELS_FULL = ["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"];
  var WEEKDAY_LABELS_SHORT = ["L","M","M","J","V","S","D"];
  function weekdayLabels(){
    return (window.innerWidth <= 420) ? WEEKDAY_LABELS_SHORT : WEEKDAY_LABELS_FULL;
  }
  var MONTH_LABELS_FR = {
    janvier:"janvier", fevrier:"février", mars:"mars", avril:"avril", mai:"mai", juin:"juin",
    juillet:"juillet", aout:"août", septembre:"septembre", octobre:"octobre",
    novembre:"novembre", decembre:"décembre"
  };
  var MONTH_SLUGS_ORDER = [
    "janvier","fevrier","mars","avril","mai","juin",
    "juillet","aout","septembre","octobre","novembre","decembre"
  ];
  var FR_MONTH_NAMES = [
    "janvier","février","mars","avril","mai","juin",
    "juillet","août","septembre","octobre","novembre","décembre"
  ];

  var state = {
    months: [],
    monthIndex: 0,
    view: "calendar",
    filter: "all",
    query: ""
  };

  var els = {
    main: document.getElementById("kjMain"),
    monthSelect: document.getElementById("kjMonthSelect"),
    prevBtn: document.getElementById("kjPrevBtn"),
    nextBtn: document.getElementById("kjNextBtn"),
    todayBtn: document.getElementById("kjTodayBtn"),
    searchInput: document.getElementById("kjSearchInput"),
    filterChips: document.getElementById("kjFilterChips"),
    viewToggle: document.querySelector(".kj-app .kj-view-toggle"),
    overlay: document.getElementById("kjOverlay"),
    drawer: document.getElementById("kjDrawer"),
    drawerDate: document.getElementById("kjDrawerDate"),
    drawerSub: document.getElementById("kjDrawerSub"),
    drawerBody: document.getElementById("kjDrawerBody"),
    drawerClose: document.getElementById("kjDrawerClose"),
    icsBtn: document.getElementById("kjIcsBtn"),
    icsOverlay: document.getElementById("kjIcsOverlay"),
    icsModal: document.getElementById("kjIcsModal"),
    icsModalClose: document.getElementById("kjIcsModalClose")
  };

  // ---- UTILITAIRES --------------------------------------------------------
  function pad2(n){ return String(n).padStart(2,"0"); }

  function escapeHtml(str){
    return String(str).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // Un événement est soit { source:"kanpai", text, links, type, ... }
  // soit { source:"japantravel", title, event_date, event_general_price,
  //        event_free, category, url, id, slug, lang }.
  function classifyEvent(evt){
    if (evt.source === "japantravel") return "jt";
    var t = evt.text || "";
    var isNonFerie = /non\s*féri/i.test(t);
    var isFerie = !isNonFerie && /féri/i.test(t);
    if (isFerie) return "ferie";
    if (evt.type === "multi-day") return "festival";
    return "autre";
  }

  var CLASS_LABEL = {
    ferie:"Jour férié",
    festival:"Festival / plusieurs jours",
    autre:"Événement",
    jt:"Événement JapanTravel"
  };

  // Texte affiché pour un événement, quelle que soit sa source.
  function eventDisplayText(evt){
    if (evt.source === "japantravel") return evt.title || "(Événement JapanTravel)";
    return evt.text;
  }

  // Liens associés, normalisés en [{text, url}], quelle que soit la source.
  // Les liens kanpai.fr ne sont volontairement pas exposés : seuls les liens
  // JapanTravel (evt.url) sont affichés.
  function eventLinks(evt){
    if (evt.source === "japantravel"){
      return evt.url ? [{ text:"En savoir plus", url: evt.url }] : [];
    }
    return [];
  }

  function formatJtPrice(evt){
    if (evt.event_free) return "Gratuit";
    if (evt.event_general_price) return evt.event_general_price;
    return null;
  }

  // "YYYY-MM-DD HH:MM:SS" -> {y,m,day,h,min}
  function parseJtDateTime(value){
    if (!value) return null;
    var parts = value.split(" ");
    var dParts = parts[0].split("-").map(Number);
    var tParts = (parts[1] || "00:00:00").split(":").map(Number);
    if (dParts.length < 3 || dParts.some(isNaN)) return null;
    return { y:dParts[0], m:dParts[1], day:dParts[2], h:tParts[0]||0, min:tParts[1]||0 };
  }

  function formatJtDate(dt, withTime){
    var s = dt.day + " " + FR_MONTH_NAMES[dt.m-1] + " " + dt.y;
    if (withTime) s += " à " + pad2(dt.h) + "h" + pad2(dt.min);
    return s;
  }

  // Résumé lisible de la période d'un événement JapanTravel (utilisé dans le drawer).
  function formatJtDateRange(evt){
    var ed = evt.event_date || {};
    var start = parseJtDateTime(ed.start);
    if (!start) return "";
    var end = parseJtDateTime(ed.end);
    var showStartTime = !ed.unknown_start_time;
    var showEndTime = end && !ed.unknown_end_time;

    var sameDay = end && end.y === start.y && end.m === start.m && end.day === start.day;

    if (!end || sameDay){
      var s = formatJtDate(start, showStartTime);
      if (showEndTime && showStartTime){
        s += " – " + pad2(end.h) + "h" + pad2(end.min);
      }
      return s;
    }
    return "Du " + formatJtDate(start, false) + " au " + formatJtDate(end, false);
  }

  function classifyEventDot(kind){ return kind; }

  function dayMatchesFilter(day){
    if (state.filter === "all") return day.events.length > 0;
    return day.events.some(function(e){ return classifyEvent(e) === state.filter; });
  }

  function eventsForDayFiltered(day){
    if (state.filter === "all") return day.events;
    return day.events.filter(function(e){ return classifyEvent(e) === state.filter; });
  }

  function parseLocalDate(iso){
    var parts = iso.split("-");
    return new Date(parseInt(parts[0],10), parseInt(parts[1],10)-1, parseInt(parts[2],10));
  }

  function todayIso(){
    var d = new Date();
    var m = String(d.getMonth()+1).padStart(2,"0");
    var day = String(d.getDate()).padStart(2,"0");
    return d.getFullYear()+"-"+m+"-"+day;
  }

  function monthLongLabel(month){
    var label = MONTH_LABELS_FR[month.month_slug] || month.month_slug;
    return label.charAt(0).toUpperCase() + label.slice(1) + " " + month.year;
  }

  // ---- FUSION DES DEUX SOURCES DE DONNÉES ---------------------------------
  function monthKey(y, m){ return y + "-" + pad2(m); }

  function monthIndexFromSlug(slug){
    var idx = MONTH_SLUGS_ORDER.indexOf(slug);
    return idx >= 0 ? idx + 1 : 1;
  }

  function daysInMonth(y, m){ return new Date(y, m, 0).getDate(); }

  function isoFromParts(y, m, d){ return y + "-" + pad2(m) + "-" + pad2(d); }

  function buildEmptyDays(y, m){
    var n = daysInMonth(y, m);
    var days = [];
    for (var d=1; d<=n; d++){
      days.push({ day:d, weekday_letter:null, date_iso:isoFromParts(y,m,d), events:[] });
    }
    return days;
  }

  // Construit une liste unifiée de mois à partir du payload brut du scraper :
  // - reprend les mois kanpai.fr tels quels (en taguant leurs événements source:"kanpai"
  //   s'ils ne le sont pas déjà — le cas --jt-merge côté script les tague déjà "japantravel"),
  // - rattache les événements japantravel_events (mode non fusionné) au bon jour, en créant
  //   au besoin un mois "synthétique" pour les dates qui tombent hors des mois scrapés par kanpai.
  function buildUnifiedMonths(payload){
    var monthsByKey = {};

    (payload.months || []).forEach(function(m){
      m.days.forEach(function(day){
        day.events = (day.events || []).map(function(e){
          if (!e.source) e.source = "kanpai";
          return e;
        });
      });
      monthsByKey[monthKey(m.year, monthIndexFromSlug(m.month_slug))] = m;
    });

    var jtEvents = payload.japantravel_events || [];
    jtEvents.forEach(function(ev){
      var start = ev.event_date && ev.event_date.start;
      var d = parseJtDateTime(start);
      if (!d) return;

      var key = monthKey(d.y, d.m);
      var month = monthsByKey[key];
      if (!month){
        month = {
          url: null,
          month_slug: MONTH_SLUGS_ORDER[d.m-1],
          year: d.y,
          title: null,
          days: buildEmptyDays(d.y, d.m),
          synthetic: true
        };
        monthsByKey[key] = month;
      }

      var day = month.days.filter(function(dd){ return dd.day === d.day; })[0];
      if (!day){
        day = { day:d.day, weekday_letter:null, date_iso:isoFromParts(d.y,d.m,d.day), events:[] };
        month.days.push(day);
        month.days.sort(function(a,b){ return a.day - b.day; });
      }

      var tagged = {};
      for (var k in ev){ if (Object.prototype.hasOwnProperty.call(ev,k)) tagged[k] = ev[k]; }
      tagged.source = "japantravel";
      day.events.push(tagged);
    });

    return Object.keys(monthsByKey).sort().map(function(k){ return monthsByKey[k]; });
  }

  // ---- CHARGEMENT DES DONNÉES ---------------------------------------------
  function loadData(){
    els.main.innerHTML = '<div class="kj-state-msg" id="kjLoadingMsg">Chargement du calendrier…</div>';
    fetch(DATA_URL, { cache: "no-store" })
      .then(function(res){
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function(data){
        var hasKanpai = data && Array.isArray(data.months) && data.months.length > 0;
        var hasJt = data && Array.isArray(data.japantravel_events) && data.japantravel_events.length > 0;
        if (!data || (!hasKanpai && !hasJt)){
          throw new Error("Format de données inattendu");
        }
        state.months = buildUnifiedMonths(data);
        if (state.months.length === 0){
          throw new Error("Aucun mois exploitable dans les données");
        }
        var todayI = todayIso();
        var idx = state.months.findIndex(function(m){
          return todayI >= m.days[0].date_iso && todayI <= m.days[m.days.length-1].date_iso;
        });
        state.monthIndex = idx >= 0 ? idx : 0;
        populateMonthSelect();
        render();
        els.icsBtn.disabled = false;
      })
      .catch(function(err){
        els.icsBtn.disabled = true;
        renderError(err);
      });
  }

  function renderError(err){
    els.main.innerHTML =
      '<div class="kj-state-msg">' +
      "Impossible de charger le calendrier pour le moment.<br>" +
      '<span style="font-size:12px;opacity:0.8;">(' + escapeHtml(err.message || String(err)) + ")</span><br>" +
      '<button class="retry" id="kjRetryBtn">Réessayer</button>' +
      "</div>";
    document.getElementById("kjRetryBtn").addEventListener("click", loadData);
  }

  // ---- NAVIGATION ---------------------------------------------------------
  function populateMonthSelect(){
    els.monthSelect.innerHTML = state.months.map(function(m, i){
      return '<option value="' + i + '">' + monthLongLabel(m) + "</option>";
    }).join("");
    els.monthSelect.value = state.monthIndex;
  }

  function goToMonth(i){
    if (i < 0 || i >= state.months.length) return;
    state.monthIndex = i;
    els.monthSelect.value = i;
    render();
  }

  function goToday(){
    var todayI = todayIso();
    var idx = state.months.findIndex(function(m){
      return todayI >= m.days[0].date_iso && todayI <= m.days[m.days.length-1].date_iso;
    });
    if (idx >= 0){
      state.query = "";
      els.searchInput.value = "";
      goToMonth(idx);
    }
  }

  // ---- RENDU PRINCIPAL ------------------------------------------------------
  function render(){
    if (state.query.trim().length > 1){
      renderSearchResults(state.query.trim());
      return;
    }
    if (state.view === "calendar"){
      renderCalendar();
    } else {
      renderAgenda();
    }
    updateNavButtons();
  }

  function updateNavButtons(){
    els.prevBtn.disabled = state.monthIndex <= 0;
    els.nextBtn.disabled = state.monthIndex >= state.months.length - 1;
  }

  function renderCalendar(){
    var month = state.months[state.monthIndex];
    var todayI = todayIso();
    var firstDate = parseLocalDate(month.days[0].date_iso);
    var offset = (firstDate.getDay() + 6) % 7; // 0 = lundi

    var html = '<h2 class="kj-month-title">' + monthLongLabel(month) + "</h2>";
    html += '<div class="kj-weekdays">' + weekdayLabels().map(function(w,i){
      var cls = i===5 ? "sat" : (i===6 ? "sun" : "");
      return '<span class="'+cls+'">' + w + "</span>";
    }).join("") + "</div>";

    html += '<div class="kj-grid">';
    for (var i=0;i<offset;i++){ html += '<div class="kj-cell empty"></div>'; }

    month.days.forEach(function(day){
      var visibleEvents = eventsForDayFiltered(day);
      var hasEvents = visibleEvents.length > 0;
      var isToday = day.date_iso === todayI;
      var cls = "kj-cell" + (hasEvents ? " has-events" : "") + (isToday ? " is-today" : "");

      var inner = '<span class="num">' + day.day + "</span>";
      visibleEvents.slice(0,2).forEach(function(e){
        var kind = classifyEvent(e);
        inner += '<span class="ev-line"><span class="kj-tagdot tag-' + kind + '"></span>' + escapeHtml(eventDisplayText(e)) + "</span>";
      });
      if (visibleEvents.length > 2){
        inner += '<span class="kj-more">+ ' + (visibleEvents.length - 2) + " autre(s)</span>";
      }

      html += '<div class="' + cls + '" data-day="' + day.day + '" role="' + (hasEvents ? 'button' : 'presentation') + '"' +
        (hasEvents ? ' tabindex="0"' : '') + '>' + inner + "</div>";
    });

    html += "</div>";

    if (!month.days.some(dayMatchesFilter)){
      html += '<p class="kj-empty-note">Aucun événement ne correspond à ce filtre pour ce mois.</p>';
    }

    els.main.innerHTML = html;

    els.main.querySelectorAll(".kj-cell.has-events").forEach(function(cell){
      cell.addEventListener("click", function(){ openDay(month, parseInt(cell.dataset.day,10)); });
      cell.addEventListener("keydown", function(ev){
        if (ev.key === "Enter" || ev.key === " "){
          ev.preventDefault();
          openDay(month, parseInt(cell.dataset.day,10));
        }
      });
    });
  }

  function renderAgenda(){
    var month = state.months[state.monthIndex];
    var todayI = todayIso();
    var daysWithEvents = month.days.filter(dayMatchesFilter);

    var html = '<h2 class="kj-month-title">' + monthLongLabel(month) + "</h2>";

    if (daysWithEvents.length === 0){
      html += '<p class="kj-empty-note">Aucun événement ne correspond à ce filtre pour ce mois.</p>';
      els.main.innerHTML = html;
      return;
    }

    html += '<div class="kj-list">';
    daysWithEvents.forEach(function(day){
      var d = parseLocalDate(day.date_iso);
      var isToday = day.date_iso === todayI;
      html += '<div class="kj-list-row" data-day="' + day.day + '">' +
        '<div class="kj-list-date">' +
          '<span class="d" style="' + (isToday ? "color:var(--shu);" : "") + '">' + day.day + "</span>" +
          '<span class="w">' + WEEKDAY_LABELS_FULL[(d.getDay()+6)%7] + "</span>" +
        "</div>" +
        '<div class="kj-list-events">' +
          eventsForDayFiltered(day).map(function(e){
            var kind = classifyEvent(e);
            return '<div class="evt"><span class="kj-tagdot tag-' + kind + '" style="margin-top:6px;"></span><span class="txt">' + escapeHtml(eventDisplayText(e)) + "</span></div>";
          }).join("") +
        "</div>" +
      "</div>";
    });
    html += "</div>";

    els.main.innerHTML = html;
    els.main.querySelectorAll(".kj-list-row").forEach(function(row){
      row.addEventListener("click", function(){ openDay(month, parseInt(row.dataset.day,10)); });
    });
  }

  function renderSearchResults(query){
    var q = query.toLowerCase();
    var results = [];
    state.months.forEach(function(month){
      month.days.forEach(function(day){
        var matches = day.events.filter(function(e){
          return eventDisplayText(e).toLowerCase().indexOf(q) !== -1 &&
            (state.filter === "all" || classifyEvent(e) === state.filter);
        });
        if (matches.length){
          results.push({ month: month, day: day, events: matches });
        }
      });
    });

    var html = '<h2 class="kj-month-title">Résultats pour « ' + escapeHtml(query) + " »</h2>";
    if (results.length === 0){
      html += '<p class="kj-empty-note">Aucun événement trouvé.</p>';
      els.main.innerHTML = html;
      return;
    }

    html += '<div class="kj-search-results kj-list">';
    var lastMonthKey = null;
    results.forEach(function(r, idx){
      var key = r.month.month_slug + r.month.year;
      if (key !== lastMonthKey){
        html += '<div class="res-month">' + monthLongLabel(r.month) + "</div>";
        lastMonthKey = key;
      }
      var d = parseLocalDate(r.day.date_iso);
      html += '<div class="kj-list-row" data-idx="' + idx + '">' +
        '<div class="kj-list-date">' +
          '<span class="d">' + r.day.day + "</span>" +
          '<span class="w">' + WEEKDAY_LABELS_FULL[(d.getDay()+6)%7] + "</span>" +
        "</div>" +
        '<div class="kj-list-events">' +
          r.events.map(function(e){
            var kind = classifyEvent(e);
            return '<div class="evt"><span class="kj-tagdot tag-' + kind + '" style="margin-top:6px;"></span><span class="txt">' + escapeHtml(eventDisplayText(e)) + "</span></div>";
          }).join("") +
        "</div>" +
      "</div>";
    });
    html += "</div>";

    els.main.innerHTML = html;
    els.main.querySelectorAll(".kj-search-results .kj-list-row").forEach(function(row){
      row.addEventListener("click", function(){
        var r = results[parseInt(row.dataset.idx,10)];
        var mi = state.months.indexOf(r.month);
        state.query = "";
        els.searchInput.value = "";
        state.monthIndex = mi;
        els.monthSelect.value = mi;
        openDay(r.month, r.day.day);
        render();
      });
    });
  }

  // ---- PANNEAU DE DÉTAIL (DRAWER) -----------------------------------------
  function openDay(month, dayNum){
    var day = month.days.find(function(d){ return d.day === dayNum; });
    if (!day) return;
    var d = parseLocalDate(day.date_iso);

    els.drawerDate.textContent = day.day + " " + monthLongLabel(month);
    els.drawerSub.textContent = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"][(d.getDay()+6)%7];

    var events = eventsForDayFiltered(day).length ? eventsForDayFiltered(day) : day.events;
    els.drawerBody.innerHTML = events.map(function(e){
      var kind = classifyEvent(e);
      var kindLabel = (kind === "jt" && e.category && e.category.name) ? e.category.name : CLASS_LABEL[kind];
      var linksArr = eventLinks(e);
      var links = linksArr.map(function(l){
        return '<a href="' + escapeHtml(l.url) + '" target="_blank" rel="noopener">' + escapeHtml(l.text || l.url) + "</a>";
      }).join("");

      var metaLine = "";
      if (kind === "jt"){
        var bits = [];
        var range = formatJtDateRange(e);
        if (range) bits.push(range);
        var price = formatJtPrice(e);
        if (price) bits.push(price);
        if (bits.length){
          metaLine = '<div class="jt-meta">' + escapeHtml(bits.join(" · ")) + "</div>";
        }
      }

      return '<div class="kj-event-card ' + kind + '">' +
        '<div class="kind">' + escapeHtml(kindLabel) + "</div>" +
        '<div class="text">' + escapeHtml(eventDisplayText(e)) + "</div>" +
        metaLine +
        (links ? '<div class="links">' + links + "</div>" : "") +
      "</div>";
    }).join("");

    els.overlay.classList.add("open");
    els.drawer.classList.add("open");
    els.drawer.setAttribute("aria-hidden","false");
  }

  function closeDrawer(){
    els.overlay.classList.remove("open");
    els.drawer.classList.remove("open");
    els.drawer.setAttribute("aria-hidden","true");
  }

  // ---- EXPORT .ICS ---------------------------------------------------------
  function isoNoDashes(iso){ return iso.replace(/-/g,""); }

  function addDaysToIso(iso, days){
    var d = parseLocalDate(iso);
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" + pad2(d.getMonth()+1) + "-" + pad2(d.getDate());
  }

  function icsLocalDateTime(dt){
    return dt.y + pad2(dt.m) + pad2(dt.day) + "T" + pad2(dt.h) + pad2(dt.min) + "00";
  }

  function escapeIcsText(str){
    return String(str)
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,")
      .replace(/\r?\n/g, "\\n");
  }

  // Découpe les lignes trop longues comme l'exige la RFC 5545 (max 75 octets)
  function foldIcsLine(line){
    if (line.length <= 74) return line;
    var out = line.slice(0,74);
    var rest = line.slice(74);
    while (rest.length > 0){
      out += "\r\n " + rest.slice(0,73);
      rest = rest.slice(73);
    }
    return out;
  }

  // Événement "journée entière" (jours fériés / festivals kanpai) : pas d'horaire connu.
  function appendKanpaiVevent(lines, day, evt, uidSuffix, dtstamp){
    var dtstart = isoNoDashes(day.date_iso);
    var dtend = isoNoDashes(addDaysToIso(day.date_iso, 1));
    lines.push("BEGIN:VEVENT");
    lines.push(foldIcsLine("UID:" + day.date_iso + "-" + uidSuffix + "@calendrier-japon"));
    lines.push("DTSTAMP:" + dtstamp);
    lines.push("DTSTART;VALUE=DATE:" + dtstart);
    lines.push("DTEND;VALUE=DATE:" + dtend);
    lines.push(foldIcsLine("SUMMARY:" + escapeIcsText(evt.text)));
    lines.push("END:VEVENT");
  }

  // Événement JapanTravel : horaires réels (heure de Tokyo) quand ils sont connus,
  // sinon repli sur un événement journée entière couvrant la période annoncée.
  function appendJtVevent(lines, evt, dtstamp){
    var ed = evt.event_date || {};
    var start = parseJtDateTime(ed.start);
    if (!start) return;
    var end = parseJtDateTime(ed.end);
    var hasStartTime = !ed.unknown_start_time;

    lines.push("BEGIN:VEVENT");
    var uid = "jt-" + (evt.id != null ? evt.id : Math.random().toString(36).slice(2));
    lines.push(foldIcsLine("UID:" + uid + "@calendrier-japon"));
    lines.push("DTSTAMP:" + dtstamp);

    if (hasStartTime){
      lines.push("DTSTART;TZID=Asia/Tokyo:" + icsLocalDateTime(start));
      var endDt = (end && !ed.unknown_end_time) ? end : null;
      if (!endDt || icsLocalDateTime(endDt) === icsLocalDateTime(start)){
        var d = new Date(start.y, start.m-1, start.day, start.h, start.min);
        d.setHours(d.getHours() + 1);
        endDt = { y:d.getFullYear(), m:d.getMonth()+1, day:d.getDate(), h:d.getHours(), min:d.getMinutes() };
      }
      lines.push("DTEND;TZID=Asia/Tokyo:" + icsLocalDateTime(endDt));
    } else {
      var startIso = isoFromParts(start.y, start.m, start.day);
      var endIso = end ? isoFromParts(end.y, end.m, end.day) : startIso;
      lines.push("DTSTART;VALUE=DATE:" + isoNoDashes(startIso));
      lines.push("DTEND;VALUE=DATE:" + isoNoDashes(addDaysToIso(endIso, 1)));
    }

    lines.push(foldIcsLine("SUMMARY:" + escapeIcsText(evt.title || "Événement JapanTravel")));

    var descParts = [];
    if (evt.category && evt.category.name) descParts.push("Catégorie : " + evt.category.name);
    var price = formatJtPrice(evt);
    if (price) descParts.push("Prix : " + price);
    if (descParts.length){
      lines.push(foldIcsLine("DESCRIPTION:" + escapeIcsText(descParts.join(" — "))));
    }
    if (evt.url){
      lines.push(foldIcsLine("URL:" + evt.url));
    }
    lines.push("END:VEVENT");
  }

  function buildIcsContent(){
    var now = new Date();
    var dtstamp = now.getUTCFullYear() + pad2(now.getUTCMonth()+1) + pad2(now.getUTCDate()) +
      "T" + pad2(now.getUTCHours()) + pad2(now.getUTCMinutes()) + pad2(now.getUTCSeconds()) + "Z";

    var lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//Calendrier des evenements du Japon//FR",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "X-WR-CALNAME:Événements du Japon"
    ];

    state.months.forEach(function(month){
      month.days.forEach(function(day){
        day.events.forEach(function(evt, i){
          if (evt.source === "japantravel"){
            appendJtVevent(lines, evt, dtstamp);
          } else {
            appendKanpaiVevent(lines, day, evt, i, dtstamp);
          }
        });
      });
    });

    lines.push("END:VCALENDAR");
    return lines.join("\r\n");
  }

  function downloadIcsFile(){
    if (!state.months.length) return;
    var content = buildIcsContent();
    var blob = new Blob([content], { type: "text/calendar;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "calendrier-japon.ics";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  }

  function openIcsModal(){
    els.icsOverlay.classList.add("open");
    els.icsModal.classList.add("open");
    els.icsModal.setAttribute("aria-hidden","false");
  }

  function closeIcsModal(){
    els.icsOverlay.classList.remove("open");
    els.icsModal.classList.remove("open");
    els.icsModal.setAttribute("aria-hidden","true");
  }

  els.icsBtn.addEventListener("click", function(){
    downloadIcsFile();
    openIcsModal();
  });
  els.icsModalClose.addEventListener("click", closeIcsModal);
  els.icsOverlay.addEventListener("click", closeIcsModal);

  // ---- ÉVÉNEMENTS UI --------------------------------------------------------
  els.prevBtn.addEventListener("click", function(){ goToMonth(state.monthIndex - 1); });
  els.nextBtn.addEventListener("click", function(){ goToMonth(state.monthIndex + 1); });
  els.todayBtn.addEventListener("click", goToday);
  els.monthSelect.addEventListener("change", function(){
    state.query = "";
    els.searchInput.value = "";
    goToMonth(parseInt(els.monthSelect.value,10));
  });

  var searchTimer = null;
  els.searchInput.addEventListener("input", function(){
    clearTimeout(searchTimer);
    var val = els.searchInput.value;
    searchTimer = setTimeout(function(){
      state.query = val;
      render();
    }, 200);
  });

  els.filterChips.addEventListener("click", function(ev){
    var btn = ev.target.closest(".kj-chip");
    if (!btn) return;
    els.filterChips.querySelectorAll(".kj-chip").forEach(function(c){ c.setAttribute("aria-pressed","false"); });
    btn.setAttribute("aria-pressed","true");
    state.filter = btn.dataset.filter;
    render();
  });

  els.viewToggle.addEventListener("click", function(ev){
    var btn = ev.target.closest("button[data-view]");
    if (!btn) return;
    els.viewToggle.querySelectorAll("button").forEach(function(b){ b.setAttribute("aria-pressed","false"); });
    btn.setAttribute("aria-pressed","true");
    state.view = btn.dataset.view;
    render();
  });

  els.drawerClose.addEventListener("click", closeDrawer);
  els.overlay.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function(ev){
    if (ev.key === "Escape"){
      closeDrawer();
      closeIcsModal();
    }
  });

  // ---- DÉMARRAGE ---------------------------------------------------------
  var wasNarrow = window.innerWidth <= 420;
  var resizeTimer = null;
  window.addEventListener("resize", function(){
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function(){
      var isNarrow = window.innerWidth <= 420;
      if (isNarrow !== wasNarrow){
        wasNarrow = isNarrow;
        if (state.months.length) render();
      }
    }, 150);
  });

  loadData();

})();
