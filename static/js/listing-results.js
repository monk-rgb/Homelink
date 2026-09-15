(() => {
    let latestAnalysis = null;
    const originalFetch = window.fetch;

    window.fetch = async (...args) => {
        const requestBody = args[1]?.body;
        const requestUrl = typeof args[0] === 'string' ? args[0] : args[0]?.url;
        if (requestUrl?.includes('/api/analyze-property-image') && requestBody instanceof FormData) {
            requestBody.append('location_hint', document.getElementById('locationHint')?.value || '');
        }
        const response = await originalFetch(...args);
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
        if (url?.includes('/api/analyze-property-image')) {
            try {
                latestAnalysis = await response.clone().json();
            } catch (error) {
                latestAnalysis = null;
            }
        }
        return response;
    };

    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[character]));

    const renderMatches = () => {
        const links = document.getElementById('listingLinks');
        if (!links || !latestAnalysis || links.dataset.onlineRendered) return;
        links.dataset.onlineRendered = 'true';
        const matches = latestAnalysis.online_matches || [];
        const heading = '<h3>Online listing price matches</h3>';
        const content = matches.length
            ? matches.map(match => `<a class="text-link listing-match" target="_blank" rel="noopener" href="${escapeHtml(match.url)}"><span>${escapeHtml(match.title)}</span><b>NGN ${Number(match.price).toLocaleString()}</b></a>`).join('')
            : `<p class="muted">${escapeHtml(latestAnalysis.online_search_message || 'No online listing prices were found.')}</p>`;
        links.insertAdjacentHTML('afterbegin', heading + content);
    };

    new MutationObserver(renderMatches).observe(document.body, { childList: true, subtree: true });
})();
