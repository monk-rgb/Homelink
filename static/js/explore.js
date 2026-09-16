/* Explore properties map.
 *
 * Reads the marker data the server rendered into #mapData (real listings with
 * approximate area coordinates) and plots them on a Leaflet map of Nigeria.
 * Clicking a marker pops a property card; the sidebar buttons recentre the map
 * on an area. No listing data is invented here - it all comes from the server.
 */
(function () {
  const el = document.getElementById('propertyMap');
  const dataEl = document.getElementById('mapData');
  let markers = [];
  try { markers = JSON.parse((dataEl && dataEl.textContent) || '[]'); } catch (e) { markers = []; }
  if (!el) return;
  // The map library is bundled locally, but a blocked/absent script must not
  // leave a blank rectangle: fall back to a readable list of the same listings.
  if (typeof L === 'undefined') {
    el.innerHTML = '<ul class="map-fallback">' + markers.map((m) =>
      '<li><strong>' + (m.name || 'Property') + '</strong><span>' + (m.location || '') +
      '</span></li>').join('') + '</ul>';
    el.classList.add('map-fallback-wrap');
    return;
  }

  const map = L.map(el, { scrollWheelZoom: true }).setView([7.5, 6.5], 6);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  const naira = (v) => '\u20a6' + Number(v || 0).toLocaleString('en-NG', { maximumFractionDigits: 0 });

  const bounds = [];
  markers.forEach((m) => {
    if (typeof m.lat !== 'number' || typeof m.lng !== 'number') return;
    const marker = L.marker([m.lat, m.lng]).addTo(map);
    const photo = m.photo_url ? `<img src="${m.photo_url}" alt="" style="width:100%;height:90px;object-fit:cover;border-radius:6px;margin-bottom:6px;">` : '';
    const badge = m.listing_type === 'rent' ? 'For Rent' : 'For Sale';
    marker.bindPopup(
      `<div style="min-width:180px;">${photo}` +
      `<strong style="display:block;font-size:13px;">${m.name || 'Property'}</strong>` +
      `<span style="color:#888;font-size:11px;">${m.location || ''} · ${badge}</span>` +
      `<div style="margin-top:6px;font-weight:700;">${naira(m.price)}</div>` +
      `<div style="color:#4ade80;font-size:12px;">AI estimate ${naira(m.ai_price)}</div></div>`
    );
    bounds.push([m.lat, m.lng]);
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });

  document.querySelectorAll('.area-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const lat = parseFloat(btn.dataset.lat), lng = parseFloat(btn.dataset.lng);
      if (!isNaN(lat) && !isNaN(lng)) map.setView([lat, lng], 11);
    });
  });
})();
