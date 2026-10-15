// =========================================================
// AUTHENOVA — Frontend Controller
// Presentation layer only. API contracts are unchanged:
//   POST /api/claims/investigate   JSON {claim, max_results: 4}
//   POST /api/media/verify-image   FormData{file} | JSON {url}
//   POST /api/media/verify-video   FormData{file}
// =========================================================
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // -----------------------------------------------------
    // Helpers
    // -----------------------------------------------------
    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function safeUrl(url) {
        try {
            const u = new URL(url, window.location.href);
            return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
        } catch (e) {
            return null;
        }
    }

    function domainOf(url) {
        try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return ''; }
    }

    function formatBytes(bytes) {
        if (!bytes && bytes !== 0) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
    }

    function toNumber(v) {
        const n = typeof v === 'number' ? v : parseFloat(v);
        return Number.isFinite(n) ? n : null;
    }

    function pctText(v) {
        const n = toNumber(v);
        return n === null ? '—' : n + '%';
    }

    function clampPct(v) {
        const n = toNumber(v);
        if (n === null) return 0;
        return Math.max(0, Math.min(100, n));
    }

    // Turn any backend/network error into a short, user-safe message.
    // Never surfaces stack traces, file paths, keys or env names.
    function friendlyError(raw, status) {
        const generic = 'Unable to process your request. Please try again.';
        if (status === 413) return 'That file is too large to upload. Try a smaller file.';
        if (status === 429) return 'Too many requests right now. Wait a moment and try again.';
        if (status === 404) return 'This service is not available right now. Check that the server is running.';
        if (status >= 500 && !raw) return 'The server ran into a problem. Please try again.';
        if (!raw || typeof raw !== 'string') return generic;

        const msg = raw.trim();
        const unsafe = /(Traceback|File "|\.py\b|line \d+|\/Users\/|\/home\/|[A-Z]:\\|site-packages|Exception:|Error\(|api[_-]?key|token|secret|\benv\b|[A-Z]+_API_KEY|tvly-|gsk_|<html|<!DOCTYPE)/i;
        if (unsafe.test(msg) || msg.length > 260) return generic;
        if (/failed to fetch|networkerror|load failed/i.test(msg)) {
            return 'Could not reach the Authenova server. Check that it is running and try again.';
        }
        return msg;
    }

    async function readJson(resp) {
        const text = await resp.text();
        try { return JSON.parse(text); } catch (e) { return null; }
    }

    function showNotice(el, title, message, retry) {
        if (!el) return;
        el.innerHTML = '';
        const body = document.createElement('div');
        const strong = document.createElement('strong');
        strong.textContent = title;
        const p = document.createElement('p');
        p.textContent = message;
        body.appendChild(strong);
        body.appendChild(p);
        if (retry) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'link-btn';
            btn.textContent = 'Try again';
            btn.addEventListener('click', retry);
            body.appendChild(btn);
        }
        el.appendChild(body);
        el.hidden = false;
    }

    function hideNotice(el) { if (el) { el.hidden = true; el.innerHTML = ''; } }

    function setBusy(btn, busy, busyLabel, idleLabel) {
        if (!btn) return;
        btn.disabled = busy;
        btn.classList.toggle('is-loading', busy);
        btn.setAttribute('aria-busy', busy ? 'true' : 'false');
        const label = btn.querySelector('.btn-label');
        if (label) label.textContent = busy ? busyLabel : idleLabel;
    }

    // -----------------------------------------------------
    // Routing (hash based: #home, #media, #claim, #about)
    // -----------------------------------------------------
    const ROUTES = ['home', 'media', 'claim', 'about'];
    const ALIASES = { claims: 'claim', architecture: 'about' };
    let currentRoute = null;

    function resolveRoute(hash) {
        let r = (hash || '').replace(/^#/, '').toLowerCase();
        if (ALIASES[r]) r = ALIASES[r];
        return ROUTES.includes(r) ? r : 'home';
    }

    function showRoute(route, opts) {
        opts = opts || {};
        if (route === currentRoute) return;
        currentRoute = route;

        document.querySelectorAll('.view').forEach((v) => {
            const match = v.dataset.view === route;
            v.hidden = !match;
            v.classList.toggle('is-active', match);
            v.classList.remove('is-entering');
            if (match && !prefersReducedMotion) {
                void v.offsetWidth;
                v.classList.add('is-entering');
            }
        });

        document.querySelectorAll('.nav-link').forEach((a) => {
            if (a.dataset.route === route) a.setAttribute('aria-current', 'page');
            else a.removeAttribute('aria-current');
        });

        const titles = {
            home: 'Authenova — Look Beyond the Surface',
            media: 'Verify Media — Authenova',
            claim: 'Investigate a Claim — Authenova',
            about: 'About — Authenova'
        };
        document.title = titles[route];

        closeMobileNav();
        if (!opts.initial) {
            window.scrollTo({ top: 0, behavior: prefersReducedMotion ? 'auto' : 'smooth' });
            const main = $('main');
            if (main) main.focus({ preventScroll: true });
        }
    }

    // Back-compat with the old global used by inline handlers
    window.switchView = function (name) {
        const r = resolveRoute(name);
        if (location.hash !== '#' + r) location.hash = r; else showRoute(r);
    };

    window.addEventListener('hashchange', () => showRoute(resolveRoute(location.hash)));

    // Mobile nav
    function closeMobileNav() {
        const links = $('nav-links');
        const toggle = $('nav-toggle');
        if (links) links.classList.remove('is-open');
        if (toggle) { toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-label', 'Open menu'); }
    }

    function initNav() {
        const toggle = $('nav-toggle');
        const links = $('nav-links');
        if (toggle && links) {
            toggle.addEventListener('click', () => {
                const open = !links.classList.contains('is-open');
                links.classList.toggle('is-open', open);
                toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
                toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
            });
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMobileNav(); });
        }
        const nav = $('nav');
        const onScroll = () => nav && nav.classList.toggle('is-scrolled', window.scrollY > 8);
        window.addEventListener('scroll', onScroll, { passive: true });
        onScroll();
    }

    // -----------------------------------------------------
    // Hero lens ("Look Beyond the Surface")
    // -----------------------------------------------------
    function initLens() {
        const hero = $('hero');
        const wrap = hero && hero.querySelector('.hero-title-wrap');
        if (!hero || !wrap) return;

        const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

        function place(xPct, yPct) {
            hero.style.setProperty('--lx', xPct + '%');
            hero.style.setProperty('--ly', yPct + '%');
        }

        if (!finePointer || prefersReducedMotion) {
            // Static lens resting over the title on touch devices / reduced motion
            place(68, 72);
            hero.classList.add('lens-on');
            return;
        }

        let raf = null;
        hero.addEventListener('pointermove', (e) => {
            const r = wrap.getBoundingClientRect();
            const x = ((e.clientX - r.left) / r.width) * 100;
            const y = ((e.clientY - r.top) / r.height) * 100;
            if (raf) cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => place(x, y));
            hero.classList.add('lens-on');
        });
        hero.addEventListener('pointerleave', () => hero.classList.remove('lens-on'));

        // One-time intro: the lens glides across the title once on load
        const start = performance.now();
        const dur = 2200;
        hero.classList.add('lens-on');
        function intro(t) {
            if (hero.dataset.userMoved) return;
            const p = Math.min(1, (t - start) / dur);
            const ease = 1 - Math.pow(1 - p, 3);
            place(8 + ease * 64, 30 + Math.sin(ease * Math.PI) * 40);
            if (p < 1) requestAnimationFrame(intro);
            else setTimeout(() => { if (!hero.matches(':hover')) hero.classList.remove('lens-on'); }, 500);
        }
        hero.addEventListener('pointermove', () => { hero.dataset.userMoved = '1'; }, { once: true });
        setTimeout(() => requestAnimationFrame(intro), 700);
    }

    // =====================================================
    // CLAIM INVESTIGATION
    // =====================================================
    let timerInterval = null;
    let stageTimeouts = [];

    function updateCharCounter() {
        const input = $('claim-input');
        const counter = $('char-counter');
        const wrap = $('char-count');
        if (!input || !counter) return;
        counter.textContent = input.value.length;
        if (wrap) wrap.classList.toggle('is-near', input.value.length > 900);
    }

    function setClaim(text) {
        const input = $('claim-input');
        if (!input) return;
        input.value = text;
        updateCharCounter();
        if (currentRoute !== 'claim') window.switchView('claim');
        input.focus();
    }
    window.setClaim = setClaim;

    // Stage pacing mirrors typical backend timing; the final stage
    // stays active until the real response arrives.
    function resetStages() {
        document.querySelectorAll('#pipeline .step').forEach((s) => s.classList.remove('is-active', 'is-done'));
    }
    function setStage(n) {
        document.querySelectorAll('#pipeline .step').forEach((s) => {
            const i = parseInt(s.dataset.step, 10);
            s.classList.toggle('is-done', i < n);
            s.classList.toggle('is-active', i === n);
            if (i === n) s.setAttribute('aria-current', 'step'); else s.removeAttribute('aria-current');
        });
    }
    function completeStages() {
        document.querySelectorAll('#pipeline .step').forEach((s) => {
            s.classList.remove('is-active');
            s.classList.add('is-done');
            s.removeAttribute('aria-current');
        });
    }
    function startStages() {
        resetStages();
        setStage(1);
        const plan = [
            { delay: 1800, stage: 2 },
            { delay: 9000, stage: 3 },
            { delay: 18000, stage: 4 },
            { delay: 27000, stage: 5 }
        ];
        stageTimeouts = plan.map((p) => setTimeout(() => setStage(p.stage), p.delay));
    }
    function stopStages() {
        stageTimeouts.forEach(clearTimeout);
        stageTimeouts = [];
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = null;
    }

    let claimInFlight = false;

    async function investigateClaim() {
        if (claimInFlight) return;
        const input = $('claim-input');
        const claim = input ? input.value.trim() : '';
        const errorEl = $('claim-error');
        const progress = $('claim-progress');
        const results = $('claim-results');
        const btn = $('investigate-btn');

        if (!claim || claim.length < 5) {
            showNotice(errorEl, 'Add a little more detail', 'Enter a claim of at least 5 characters to investigate.');
            if (input) input.focus();
            return;
        }
        if (claim.length > 1000) {
            showNotice(errorEl, 'Claim is too long', 'Shorten the claim to 1000 characters or fewer.');
            return;
        }

        hideNotice(errorEl);
        if (results) { results.hidden = true; results.classList.remove('is-revealed'); }

        claimInFlight = true;
        setBusy(btn, true, 'Investigating…', 'Investigate Claim');

        if ($('progress-claim')) $('progress-claim').textContent = '“' + claim + '”';
        if (progress) progress.hidden = false;

        const timerEl = $('progress-timer');
        const startTime = Date.now();
        if (timerInterval) clearInterval(timerInterval);
        timerInterval = setInterval(() => {
            if (timerEl) timerEl.textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
        }, 100);
        startStages();

        if (progress) progress.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'center' });

        try {
            let response;
            try {
                response = await fetch('/api/claims/investigate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    body: JSON.stringify({ claim: claim, max_results: 4 })
                });
            } catch (netErr) {
                throw { friendly: 'Could not reach the Authenova server. Check that it is running and try again.' };
            }

            const data = await readJson(response);
            if (!response.ok || !data) {
                const raw = data && (data.message || data.error);
                throw { friendly: friendlyError(raw, response.status) };
            }

            stopStages();
            completeStages();

            setTimeout(() => {
                if (progress) progress.hidden = true;
                renderClaimResults(data, claim);
                if (results) {
                    results.hidden = false;
                    if (!prefersReducedMotion) {
                        void results.offsetWidth;
                        results.classList.add('is-revealed');
                    }
                    const card = $('verdict-card');
                    if (card) {
                        card.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
                        card.focus({ preventScroll: true });
                    }
                }
            }, 450);
        } catch (err) {
            stopStages();
            if (progress) progress.hidden = true;
            const msg = (err && err.friendly) || friendlyError(err && err.message);
            showNotice(errorEl, 'Something went wrong', msg, investigateClaim);
        } finally {
            claimInFlight = false;
            setBusy(btn, false, 'Investigating…', 'Investigate Claim');
        }
    }
    window.investigateClaim = investigateClaim;

    function verdictTone(verdict) {
        const v = String(verdict || '').toLowerCase();
        if (v.indexOf('support') !== -1) return 'is-supported';
        if (v.indexOf('contradict') !== -1 || v.indexOf('refut') !== -1) return 'is-contradicted';
        return 'is-inconclusive';
    }

    function renderReasoning(container, text) {
        container.innerHTML = '';
        const raw = String(text || '').trim();
        if (!raw) {
            const p = document.createElement('p');
            p.textContent = 'No reasoning was provided for this result.';
            container.appendChild(p);
            return;
        }
        // Respect explicit paragraphs; otherwise group long text into
        // short paragraphs of ~2 sentences so it's easy to scan.
        let paras = raw.split(/\n\s*\n|\n/).map((s) => s.trim()).filter(Boolean);
        if (paras.length === 1 && raw.length > 320) {
            const sentences = raw.match(/[^.!?]+[.!?]+(\s+|$)|[^.!?]+$/g) || [raw];
            paras = [];
            for (let i = 0; i < sentences.length; i += 2) {
                paras.push(sentences.slice(i, i + 2).join('').trim());
            }
        }
        paras.forEach((t) => {
            const p = document.createElement('p');
            p.textContent = t;
            container.appendChild(p);
        });
    }

    function createEvidenceCard(item) {
        const card = document.createElement('article');
        card.className = 'ev-card';

        const domain = item.domain || domainOf(item.url) || 'Source';
        const title = item.source_title || domain;

        const top = document.createElement('div');
        top.className = 'ev-top';

        const mono = document.createElement('span');
        mono.className = 'ev-mono';
        mono.setAttribute('aria-hidden', 'true');
        mono.textContent = (domain.replace(/[^a-z0-9]/gi, '')[0] || '•');

        const meta = document.createElement('div');
        meta.className = 'ev-meta';
        const h = document.createElement('h3');
        h.className = 'ev-title';
        h.textContent = title;
        const d = document.createElement('p');
        d.className = 'ev-domain';
        d.textContent = domain;
        meta.appendChild(h);
        meta.appendChild(d);

        top.appendChild(mono);
        top.appendChild(meta);

        if (item.strength) {
            const s = document.createElement('span');
            s.className = 'ev-strength';
            s.textContent = item.strength;
            top.appendChild(s);
        }

        card.appendChild(top);

        if (item.evidence_sentence) {
            const q = document.createElement('blockquote');
            q.className = 'ev-quote';
            q.textContent = item.evidence_sentence;
            card.appendChild(q);
        }

        const href = safeUrl(item.url);
        if (href) {
            const foot = document.createElement('div');
            foot.className = 'ev-foot';
            const a = document.createElement('a');
            a.className = 'ev-link';
            a.href = href;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.innerHTML = 'Open source <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>';
            a.setAttribute('aria-label', 'Open source: ' + title + ' (opens in a new tab)');
            foot.appendChild(a);
            card.appendChild(foot);
        }
        return card;
    }

    function renderEvidenceList(listEl, countEl, items, emptyText) {
        if (countEl) countEl.textContent = items.length;
        if (!listEl) return;
        listEl.innerHTML = '';
        if (!items.length) {
            const e = document.createElement('div');
            e.className = 'ev-empty';
            e.textContent = emptyText;
            listEl.appendChild(e);
            return;
        }
        items.forEach((it) => listEl.appendChild(createEvidenceCard(it || {})));
    }

    function renderClaimResults(data, submittedClaim) {
        const verdict = data.verdict || 'Inconclusive';
        const card = $('verdict-card');
        if (card) {
            card.classList.remove('is-supported', 'is-contradicted', 'is-inconclusive');
            card.classList.add(verdictTone(verdict));
        }
        $('verdict-text').textContent = verdict;
        $('verdict-strength').textContent = data.evidence_strength || '';
        $('result-claim-text').textContent = data.claim || submittedClaim || '';

        const counts = data.counts || {};
        const sup = toNumber(counts.supporting) || 0;
        const con = toNumber(counts.contradicting) || 0;
        const neu = toNumber(counts.neutral) || 0;
        $('count-supporting').textContent = sup;
        $('count-contradicting').textContent = con;
        $('count-neutral').textContent = neu;

        const bar = $('tally-bar');
        if (bar) {
            const total = sup + con + neu;
            bar.classList.toggle('is-empty', total === 0);
            const spans = bar.querySelectorAll('span');
            spans.forEach((s) => { s.style.flexGrow = 0; });
            requestAnimationFrame(() => {
                spans[0].style.flexGrow = sup;
                spans[1].style.flexGrow = con;
                spans[2].style.flexGrow = neu;
            });
        }

        renderReasoning($('result-reasoning'), data.reasoning);

        renderEvidenceList(
            $('supporting-evidence-list'), $('supporting-badge-count'),
            Array.isArray(data.supporting_evidence) ? data.supporting_evidence : [],
            'No supporting evidence was found for this claim.'
        );
        renderEvidenceList(
            $('contradicting-evidence-list'), $('contradicting-badge-count'),
            Array.isArray(data.contradicting_evidence) ? data.contradicting_evidence : [],
            'No contradicting evidence was found for this claim.'
        );

        // Sources
        const sourcesEl = $('sources-container');
        const sources = Array.isArray(data.sources) ? data.sources : [];
        if (sourcesEl) {
            sourcesEl.innerHTML = '';
            const usable = sources.filter((s) => s && safeUrl(s.url));
            if (!usable.length) {
                const li = document.createElement('li');
                li.className = 'src-empty';
                li.textContent = 'No sources were returned for this investigation.';
                sourcesEl.appendChild(li);
            } else {
                usable.forEach((s) => {
                    const li = document.createElement('li');
                    const a = document.createElement('a');
                    a.href = safeUrl(s.url);
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    const dom = s.domain || domainOf(s.url);
                    a.innerHTML =
                        '<span class="src-text"><span class="src-title">' + escapeHtml(s.title || dom) +
                        '</span><span class="src-domain">' + escapeHtml(dom) + '</span></span>';
                    a.setAttribute('aria-label', (s.title || dom) + ' (opens in a new tab)');
                    li.appendChild(a);
                    sourcesEl.appendChild(li);
                });
            }
        }

        renderFactChecks(Array.isArray(data.fact_checks) ? data.fact_checks : []);

        const unc = $('result-uncertainty');
        if (unc) {
            unc.textContent = data.uncertainty || '';
            unc.hidden = !data.uncertainty;
        }
    }

    function renderFactChecks(items) {
        const wrap = $('factchecks');
        const list = $('factchecks-list');
        if (!wrap || !list) return;
        list.innerHTML = '';
        const usable = items.filter((f) => f && safeUrl(f.url));
        wrap.hidden = usable.length === 0;
        usable.forEach((f) => {
            const tone = f.stance === 'support' ? 'fc-support' : f.stance === 'contradict' ? 'fc-contra' : 'fc-neutral';
            const effect = f.stance === 'support' ? 'Backs your claim' : f.stance === 'contradict' ? 'Rejects your claim' : 'Mixed or unclear';
            const li = document.createElement('li');
            li.className = 'fc-item ' + tone;

            const top = document.createElement('div');
            top.className = 'fc-top';
            const pub = document.createElement('span');
            pub.className = 'fc-publisher';
            pub.textContent = f.publisher || f.site || 'Fact-checker';
            const rating = document.createElement('span');
            rating.className = 'fc-rating';
            rating.textContent = f.rating || 'Rated';
            top.appendChild(pub);
            top.appendChild(rating);

            const q = document.createElement('blockquote');
            q.className = 'fc-claim';
            q.textContent = f.checked_claim || '';

            const meta = document.createElement('div');
            meta.className = 'fc-meta';
            const eff = document.createElement('span');
            eff.className = 'fc-effect';
            eff.textContent = effect + (f.relation === 'opposite' ? ' (reviewed claim says the opposite)' : '');
            meta.appendChild(eff);
            if (f.review_date) {
                const d = document.createElement('span');
                d.textContent = 'Reviewed ' + f.review_date;
                meta.appendChild(d);
            }
            const a = document.createElement('a');
            a.className = 'ev-link';
            a.href = safeUrl(f.url);
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.innerHTML = 'Read fact-check <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>';
            a.setAttribute('aria-label', 'Read the ' + (f.publisher || '') + ' fact-check (opens in a new tab)');
            meta.appendChild(a);

            li.appendChild(top);
            if (f.checked_claim) li.appendChild(q);
            li.appendChild(meta);
            list.appendChild(li);
        });
    }

    function initClaims() {
        const form = $('claim-form');
        const input = $('claim-input');
        if (form) {
            form.addEventListener('submit', (e) => { e.preventDefault(); investigateClaim(); });
        }
        if (input) {
            input.addEventListener('input', updateCharCounter);
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); investigateClaim(); }
            });
        }
        document.querySelectorAll('[data-sample]').forEach((chip) => {
            chip.addEventListener('click', () => setClaim(chip.dataset.sample));
        });
        const again = $('new-claim-btn');
        if (again) {
            again.addEventListener('click', () => {
                const results = $('claim-results');
                if (results) results.hidden = true;
                if (input) { input.value = ''; updateCharCounter(); }
                window.scrollTo({ top: 0, behavior: prefersReducedMotion ? 'auto' : 'smooth' });
                if (input) input.focus({ preventScroll: true });
            });
        }
        updateCharCounter();
    }

    // =====================================================
    // MEDIA VERIFICATION
    // =====================================================
    let selectedImageFile = null;
    let selectedVideoFile = null;
    let imagePreviewUrl = null;
    let videoPreviewUrl = null;

    function switchMediaSubtab(name) {
        const seg = document.querySelector('.segmented');
        if (seg) seg.dataset.active = name;
        ['image', 'video'].forEach((t) => {
            const tab = $('tab-' + t);
            const panel = $('panel-' + t);
            const on = t === name;
            if (tab) {
                tab.classList.toggle('is-active', on);
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
            }
            if (panel) panel.hidden = !on;
        });
    }
    window.switchMediaSubtab = switchMediaSubtab;

    function initTabs() {
        const tabs = Array.from(document.querySelectorAll('[data-media-tab]'));
        tabs.forEach((tab, i) => {
            tab.addEventListener('click', () => switchMediaSubtab(tab.dataset.mediaTab));
            tab.addEventListener('keydown', (e) => {
                if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                e.preventDefault();
                const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
                switchMediaSubtab(next.dataset.mediaTab);
                next.focus();
            });
        });
    }

    function isImage(file) { return file && /^image\//.test(file.type); }
    function isVideo(file) { return file && (/^video\//.test(file.type) || /\.(mp4|avi|mov|mkv|webm)$/i.test(file.name)); }

    function setMediaFile(kind, file) {
        const zone = $(kind + '-dropzone');
        const empty = $(kind + '-dz-empty');
        const prev = $(kind + '-dz-preview');
        const chip = $(kind + '-file-chip');
        const errEl = $(kind + '-error');
        hideNotice(errEl);

        if (kind === 'image') {
            if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
            imagePreviewUrl = null;
            selectedImageFile = file || null;
        } else {
            if (videoPreviewUrl) URL.revokeObjectURL(videoPreviewUrl);
            videoPreviewUrl = null;
            selectedVideoFile = file || null;
        }

        if (!file) {
            if (zone) { zone.classList.remove('has-file'); zone.setAttribute('aria-label', 'Choose a file to upload'); }
            if (empty) empty.hidden = false;
            if (prev) prev.hidden = true;
            if (chip) chip.hidden = true;
            const media = $(kind + '-preview');
            if (media) { media.removeAttribute('src'); if (kind === 'video') media.load(); }
            const fi = $(kind + '-file-input');
            if (fi) fi.value = '';
            return;
        }

        const url = URL.createObjectURL(file);
        if (kind === 'image') imagePreviewUrl = url; else videoPreviewUrl = url;

        const media = $(kind + '-preview');
        if (media) media.src = url;
        if (zone) { zone.classList.add('has-file'); zone.setAttribute('aria-label', 'Selected ' + file.name + '. Press Enter to choose a different file.'); }
        if (empty) empty.hidden = true;
        if (prev) prev.hidden = false;
        if (chip) chip.hidden = false;
        $(kind + '-file-name').textContent = file.name;
        $(kind + '-file-meta').textContent = formatBytes(file.size) + (file.type ? ' · ' + file.type.replace(/^\w+\//, '').toUpperCase() : '');

        // A chosen file takes priority over a URL for images
        if (kind === 'image') {
            const urlInput = $('image-url-input');
            if (urlInput) urlInput.value = '';
        }
    }

    function previewImageUrl(value) {
        const empty = $('image-dz-empty');
        const prev = $('image-dz-preview');
        const img = $('image-preview');
        const zone = $('image-dropzone');
        const href = safeUrl(value);
        if (selectedImageFile) return;
        if (!href || !/^https?:\/\/.+\..+/.test(value)) {
            if (empty) empty.hidden = false;
            if (prev) prev.hidden = true;
            if (zone) zone.classList.remove('has-file');
            return;
        }
        if (img) {
            img.onerror = () => { if (empty) empty.hidden = false; if (prev) prev.hidden = true; if (zone) zone.classList.remove('has-file'); };
            img.onload = () => { if (empty) empty.hidden = true; if (prev) prev.hidden = false; if (zone) zone.classList.add('has-file'); };
            img.referrerPolicy = 'no-referrer';
            img.src = href;
        }
    }

    function initDropzone(kind) {
        const zone = $(kind + '-dropzone');
        const input = $(kind + '-file-input');
        if (!zone || !input) return;
        const accept = kind === 'image' ? isImage : isVideo;
        const typeLabel = kind === 'image' ? 'an image (JPG, PNG or WEBP)' : 'a video (MP4, AVI or MOV)';

        const open = () => input.click();
        zone.addEventListener('click', (e) => {
            // Don't hijack clicks on the video's own controls
            if (e.target && e.target.tagName === 'VIDEO') return;
            open();
        });
        zone.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        input.addEventListener('change', () => {
            const f = input.files && input.files[0];
            if (!f) return;
            if (!accept(f)) {
                showNotice($(kind + '-error'), 'Unsupported file', 'Choose ' + typeLabel + '.');
                input.value = '';
                return;
            }
            setMediaFile(kind, f);
        });

        let depth = 0;
        zone.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; zone.classList.add('is-dragover'); });
        zone.addEventListener('dragover', (e) => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'; });
        zone.addEventListener('dragleave', (e) => { e.preventDefault(); depth = Math.max(0, depth - 1); if (!depth) zone.classList.remove('is-dragover'); });
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            depth = 0;
            zone.classList.remove('is-dragover');
            const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (!f) return;
            if (!accept(f)) {
                showNotice($(kind + '-error'), 'Unsupported file', 'Choose ' + typeLabel + '.');
                return;
            }
            setMediaFile(kind, f);
        });

        const clear = $(kind + '-clear-btn');
        if (clear) clear.addEventListener('click', (e) => { e.stopPropagation(); setMediaFile(kind, null); zone.focus(); });
    }

    function mediaState(kind, state) {
        // state: 'empty' | 'loading' | 'result'
        $(kind + '-empty').hidden = state !== 'empty';
        $(kind + '-loading').hidden = state !== 'loading';
        $(kind + '-result').hidden = state !== 'result';
        const zone = $(kind + '-dropzone');
        if (zone) zone.classList.toggle('is-processing', state === 'loading');
    }

    function setBar(id, value) {
        const el = $(id);
        if (!el) return;
        el.style.width = '0%';
        requestAnimationFrame(() => requestAnimationFrame(() => { el.style.width = clampPct(value) + '%'; }));
    }

    let imageInFlight = false;
    async function verifyImageMedia() {
        if (imageInFlight) return;
        const urlInput = $('image-url-input');
        const mediaUrl = urlInput ? urlInput.value.trim() : '';
        const btn = $('verify-image-btn');
        const errEl = $('image-error');

        if (!selectedImageFile && !mediaUrl) {
            showNotice(errEl, 'Nothing to analyze yet', 'Choose an image file or paste an image URL.');
            return;
        }
        if (!selectedImageFile && !safeUrl(mediaUrl)) {
            showNotice(errEl, 'Check the URL', 'Enter a full image URL starting with http:// or https://.');
            if (urlInput) urlInput.focus();
            return;
        }

        hideNotice(errEl);
        imageInFlight = true;
        setBusy(btn, true, 'Analyzing image…', 'Verify Image');
        mediaState('image', 'loading');

        try {
            let resp;
            try {
                if (selectedImageFile) {
                    const formData = new FormData();
                    formData.append('file', selectedImageFile);
                    resp = await fetch('/api/media/verify-image', { method: 'POST', body: formData });
                } else {
                    resp = await fetch('/api/media/verify-image', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ url: mediaUrl })
                    });
                }
            } catch (netErr) {
                throw { friendly: 'Could not reach the media service. Check that it is running and try again.' };
            }

            const data = await readJson(resp);
            if (!resp.ok || !data) {
                const raw = data && (data.message || data.error);
                throw { friendly: friendlyError(raw, resp.status) };
            }

            renderImageResult(data);
            mediaState('image', 'result');
            const card = $('image-result');
            if (card && window.matchMedia('(max-width: 1000px)').matches) {
                card.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
            }
        } catch (err) {
            mediaState('image', 'empty');
            showNotice(errEl, 'Something went wrong', (err && err.friendly) || friendlyError(err && err.message), verifyImageMedia);
        } finally {
            imageInFlight = false;
            setBusy(btn, false, 'Analyzing image…', 'Verify Image');
        }
    }
    window.verifyImageMedia = verifyImageMedia;

    function renderImageResult(data) {
        const card = $('image-result');
        card.classList.remove('is-authentic', 'is-manipulated');
        card.classList.add(data.is_manipulated ? 'is-manipulated' : 'is-authentic');
        $('image-verdict').textContent = data.verdict || (data.is_manipulated ? 'AI-Generated / Manipulated' : 'Authentic Media');

        const scores = data.scores || {};
        $('img-authentic-pct').textContent = pctText(scores.authentic_percent);
        $('img-ai-pct').textContent = pctText(scores.ai_generated_percent);
        $('img-deepfake-pct').textContent = pctText(scores.deepfake_percent);
        setBar('img-authentic-bar', scores.authentic_percent);
        setBar('img-ai-bar', scores.ai_generated_percent);
        setBar('img-deepfake-bar', scores.deepfake_percent);

        const block = $('image-signatures');
        const list = $('image-signature-list');
        const sources = Array.isArray(data.sources) ? data.sources : [];
        list.innerHTML = '';
        if (sources.length) {
            block.hidden = false;
            sources.forEach((s) => {
                const li = document.createElement('li');
                const name = String(s.name || '').replace(/_/g, ' ');
                li.innerHTML =
                    '<span class="sig-name">' + escapeHtml(name) + '</span>' +
                    '<span class="sig-score">' + escapeHtml(pctText(s.score)) + '</span>' +
                    '<span class="score-bar"><span class="bar-fill"></span></span>';
                list.appendChild(li);
                const fill = li.querySelector('.bar-fill');
                requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = clampPct(s.score) + '%'; }));
            });
        } else {
            block.hidden = true;
        }
    }

    // Grab evenly spaced still frames from the selected video in the browser.
    // Returns [] if the browser can't decode the file (server then falls back).
    function captureFrames(url, count) {
        return new Promise((resolve) => {
            if (!url) return resolve([]);
            const v = document.createElement('video');
            v.muted = true; v.playsInline = true; v.preload = 'auto'; v.src = url;
            const frames = [];
            let done = false;
            const finish = () => { if (!done) { done = true; v.removeAttribute('src'); v.load(); resolve(frames); } };
            const guard = setTimeout(finish, 20000);
            v.onerror = () => { clearTimeout(guard); finish(); };
            v.onloadeddata = async () => {
                const dur = isFinite(v.duration) && v.duration > 0 ? v.duration : 0;
                if (!dur || !v.videoWidth) { clearTimeout(guard); return finish(); }
                const scale = Math.min(1, 768 / Math.max(v.videoWidth, v.videoHeight));
                const c = document.createElement('canvas');
                c.width = Math.round(v.videoWidth * scale); c.height = Math.round(v.videoHeight * scale);
                const ctx = c.getContext('2d');
                for (let i = 0; i < count; i++) {
                    const t = Math.min(dur - 0.05, (i + 0.5) * dur / count);
                    await new Promise((r) => { const to = setTimeout(r, 4000); v.onseeked = () => { clearTimeout(to); r(); }; v.currentTime = Math.max(0, t); });
                    try {
                        ctx.drawImage(v, 0, 0, c.width, c.height);
                        const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.88));
                        if (blob) frames.push(blob);
                    } catch (e) { /* skip frame */ }
                }
                clearTimeout(guard);
                finish();
            };
        });
    }

    let videoInFlight = false;
    async function verifyVideoMedia() {
        if (videoInFlight) return;
        const btn = $('verify-video-btn');
        const errEl = $('video-error');

        if (!selectedVideoFile) {
            showNotice(errEl, 'Nothing to analyze yet', 'Choose a video file to verify.');
            return;
        }

        hideNotice(errEl);
        videoInFlight = true;
        setBusy(btn, true, 'Analyzing video…', 'Verify Video');
        mediaState('video', 'loading');

        try {
            let resp;
            try {
                const formData = new FormData();
                formData.append('file', selectedVideoFile);
                const frames = await captureFrames(videoPreviewUrl, 6);
                frames.forEach((b, i) => formData.append('frame', b, 'frame' + (i + 1) + '.jpg'));
                resp = await fetch('/api/media/verify-video', { method: 'POST', body: formData });
            } catch (netErr) {
                throw { friendly: 'Could not reach the media service. Check that it is running and try again.' };
            }

            const data = await readJson(resp);
            if (!resp.ok || !data) {
                const raw = data && (data.message || data.error);
                throw { friendly: friendlyError(raw, resp.status) };
            }

            const card = $('video-result');
            card.classList.remove('is-authentic', 'is-manipulated');
            card.classList.add(data.is_manipulated ? 'is-manipulated' : 'is-authentic');
            $('video-verdict').textContent = data.verdict || (data.is_manipulated ? 'Manipulated Video' : 'Authentic Video');
            $('video-processed-filename').textContent = data.filename || selectedVideoFile.name;
            $('video-raw-classification').textContent = data.prediction || '—';
            mediaState('video', 'result');
            if (window.matchMedia('(max-width: 1000px)').matches) {
                card.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
            }
        } catch (err) {
            mediaState('video', 'empty');
            showNotice(errEl, 'Something went wrong', (err && err.friendly) || friendlyError(err && err.message), verifyVideoMedia);
        } finally {
            videoInFlight = false;
            setBusy(btn, false, 'Analyzing video…', 'Verify Video');
        }
    }
    window.verifyVideoMedia = verifyVideoMedia;

    function initMedia() {
        initTabs();
        initDropzone('image');
        initDropzone('video');
        const vi = $('verify-image-btn');
        if (vi) vi.addEventListener('click', verifyImageMedia);
        const vv = $('verify-video-btn');
        if (vv) vv.addEventListener('click', verifyVideoMedia);

        const urlInput = $('image-url-input');
        if (urlInput) {
            let t = null;
            urlInput.addEventListener('input', () => {
                if (urlInput.value.trim() && selectedImageFile) setMediaFile('image', null);
                clearTimeout(t);
                t = setTimeout(() => previewImageUrl(urlInput.value.trim()), 400);
            });
            urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); verifyImageMedia(); } });
        }
    }

    // -----------------------------------------------------
    // Boot
    // -----------------------------------------------------
    document.addEventListener('DOMContentLoaded', () => {
        initNav();
        initLens();
        initClaims();
        initMedia();
        showRoute(resolveRoute(location.hash), { initial: true });
    });
})();
