(function () {
    const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));

    function linkDots(p) {
        const max = Number(p.max_links || 10);
        const on = Number(p.links || 0);
        let dots = '';
        for (let i = 0; i < max; i++) dots += '<span class="link-dot ' + (i < on ? 'on' : '') + '"></span>';
        return '<div class="handyman-links">' + dots + '<small>' + on + '/' + max + ' links</small></div>';
    }

    function resultCard(p) {
        return '<article class="handyman-card">' +
            '<div class="handyman-card-head"><div class="handyman-avatar">' + esc((p.name || 'H')[0]).toUpperCase() + '</div>' +
            '<div><h3>' + esc(p.name || 'Handyman') + (p.star ? ' <span class="handyman-star">★</span>' : '') + '</h3>' +
            '<span class="handyman-trade">' + esc(p.trade || '') + '</span></div></div>' +
            linkDots(p) +
            '<p class="handyman-meta">' + (p.average_rating ? '★ ' + esc(p.average_rating) + '/5 · ' : '') + esc(p.jobs_completed || 0) + ' jobs delivered</p>' +
            '<p class="handyman-meta">' + esc(p.service_area || p.city || p.state || 'Location not set') + ' · ' + esc((p.pay_range || '').charAt(0).toUpperCase() + (p.pay_range || '').slice(1)) + ' pay</p>' +
            (p.bio ? '<p class="handyman-bio">' + esc(p.bio) + '</p>' : '') +
            '<div class="handyman-card-foot"><span class="badge ' + (p.available ? 'badge-green' : 'badge-muted') + '">' + (p.available ? 'Available' : 'Unavailable') + '</span>' +
            '<a class="text-link" href="' + esc(p.url) + '">View profile →</a></div></article>';
    }

    const aiForm = document.getElementById('aiSearchForm');
    const aiOut = document.getElementById('aiSearchResults');
    if (aiForm) {
        aiForm.onsubmit = async (e) => {
            e.preventDefault();
            const query = document.getElementById('aiSearchInput').value.trim();
            if (!query) return;
            aiOut.hidden = false;
            aiOut.innerHTML = '<p class="muted">Searching handyman profiles…</p>';
            try {
                const r = await fetch('/api/handyman-ai-search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query })
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Search failed');
                if (!d.results || !d.results.length) {
                    aiOut.innerHTML = '<p class="muted">No handyman matched that description yet. Try rephrasing, or browse below.</p>';
                    return;
                }
                aiOut.innerHTML = '<div class="results-head"><h2>AI matched ' + d.count + (d.count === 1 ? ' handyman' : ' handymen') + '</h2>' +
                    '<small class="muted">' + (d.source === 'openai' ? 'Ranked by AI' : 'Ranked by local matcher') + '</small></div>' +
                    '<div class="handyman-grid">' + d.results.map(resultCard).join('') + '</div>';
            } catch (err) {
                aiOut.innerHTML = '<div class="error-estimate">' + esc(err.message) + '</div>';
            }
        };
    }

    // Hire button: reveal contact details and the WhatsApp chat option.
    const hireBtn = document.getElementById('hireBtn');
    if (hireBtn) {
        hireBtn.onclick = () => {
            const box = document.getElementById('hireContact');
            if (!box) return;
            box.hidden = !box.hidden;
            hireBtn.textContent = box.hidden ? 'Hire →' : 'Hide contact details';
        };
    }

    const reviewForm = document.getElementById('reviewForm');
    if (reviewForm) {
        reviewForm.onsubmit = async (e) => {
            e.preventDefault();
            const note = document.getElementById('reviewNote');
            const fd = new FormData(reviewForm);
            note.textContent = 'Submitting…';
            try {
                const r = await fetch('/api/handyman/reviews', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        handyman_user_id: reviewForm.dataset.handyman,
                        rating: fd.get('rating'),
                        comment: fd.get('comment')
                    })
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Could not submit review');
                note.textContent = 'Thank you — your review was recorded.';
                reviewForm.reset();
                setTimeout(() => location.reload(), 900);
            } catch (err) {
                note.textContent = err.message;
            }
        };
    }
})();
