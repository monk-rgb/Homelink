/* Property detail page: photo gallery lightbox + location map.
 *
 * The gallery swaps the main image when a thumbnail is clicked and opens a
 * fullscreen viewer on clicking the main image. The map plots the listing area
 * from the coordinates the server matched to its location text.
 */
(function () {
  const main = document.getElementById('detailMain');
  document.querySelectorAll('.detail-thumb').forEach((btn) => {
    btn.addEventListener('click', () => { if (main) main.src = btn.dataset.src; });
  });
  if (main) {
    main.style.cursor = 'zoom-in';
    main.addEventListener('click', () => {
      const overlay = document.createElement('div');
      overlay.className = 'lightbox';
      overlay.innerHTML = '<img src="' + main.src + '" alt="">';
      overlay.addEventListener('click', () => overlay.remove());
      document.body.appendChild(overlay);
    });
  }

  const mapEl = document.getElementById('detailMap');
  const dataEl = document.getElementById('detailMapData');
  if (!mapEl) return;
  let data = {};
  try { data = JSON.parse((dataEl && dataEl.textContent) || '{}'); } catch (e) { data = {}; }
  if (typeof L === 'undefined') { mapEl.innerHTML = '<p class="muted" style="padding:16px;">' + (data.location || '') + '</p>'; return; }
  // Coordinates mirror the server-side CITY_COORDS anchors, matched on the
  // listing's location text.
  const COORDS = {
    lagos:[6.5244,3.3792], lekki:[6.4698,3.5852], ikoyi:[6.4541,3.4348],
    'victoria island':[6.4281,3.4219], ikeja:[6.5965,3.3421], ajah:[6.4667,3.5667],
    ikorodu:[6.6194,3.5105], surulere:[6.4994,3.3546], yaba:[6.5095,3.3711],
    abuja:[9.0765,7.3986], wuse:[9.0761,7.4589], maitama:[9.0868,7.4951],
    gwarinpa:[9.1,7.4], asokoro:[9.04,7.52], ibadan:[7.3775,3.947],
    'port harcourt':[4.8156,7.0498], enugu:[6.5244,7.5106], kano:[12.0022,8.592],
    'benin city':[6.335,5.6037], kaduna:[10.5222,7.4383], jos:[9.8965,8.8583],
    abeokuta:[7.1557,3.3451], owerri:[5.4836,7.0332], uyo:[5.0378,7.9128],
    calabar:[4.9757,8.3417], ilorin:[8.4966,4.5421], nnewi:[6.01,6.92],
    okene:[7.55,6.2333], warri:[5.5167,5.75], akure:[7.2571,5.2058]
  };
  const loc = String(data.location || '').toLowerCase();
  let coords = null;
  for (const key of Object.keys(COORDS)) { if (loc.indexOf(key) !== -1) { coords = COORDS[key]; break; } }
  const map = L.map(mapEl, { scrollWheelZoom: false }).setView(coords || [7.5, 6.5], coords ? 12 : 6);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
  if (coords) L.marker(coords).addTo(map).bindPopup(data.name || 'Property');
})();
