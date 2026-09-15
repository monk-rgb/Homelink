const searchToggle = document.getElementById('searchToggle'), tools = document.getElementById('managerTools'), search = document.getElementById('propertySearch'), notificationToggle = document.getElementById('notificationToggle'), notifications = document.getElementById('notificationPanel');
if (searchToggle && tools) { searchToggle.onclick = () => { const open = tools.hidden; tools.hidden = !open; searchToggle.setAttribute('aria-expanded', String(open)); if (open) search.focus() }; search.oninput = () => { const query = search.value.trim().toLowerCase(); document.querySelectorAll('#propertyGrid .property-card').forEach(card => { card.hidden = query && !card.textContent.toLowerCase().includes(query) }) } }
if (notificationToggle && notifications) notificationToggle.onclick = () => { const open = notifications.hidden; notifications.hidden = !open; notificationToggle.setAttribute('aria-expanded', String(open)) };
const ai = document.getElementById('aiGenerate'); if (ai) ai.onclick = async () => { const out = document.getElementById('aiOutput'); ai.disabled = true; ai.textContent = 'Preparing draft…'; out.textContent = ''; try { const r = await fetch('/api/manager-ai', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task: document.getElementById('aiTask').value, details: document.getElementById('aiDetails').value }) }); const d = await r.json(); out.textContent = d.text || d.error || 'No draft was returned.'; if (!r.ok) out.classList.add('is-error') } catch (e) { out.textContent = 'We could not connect. Check your connection and try again.'; out.classList.add('is-error') } finally { ai.disabled = false; ai.textContent = 'Generate document' } };
const pf = document.getElementById('propertyForm'), editId = document.getElementById('editPropertyId'), propertyDialog = document.getElementById('addProperty');
const publishCheck = document.getElementById('publishToPropkonetCheck'), extraFields = document.getElementById('propkonetExtraFields');
if (publishCheck && extraFields) {
    publishCheck.addEventListener('change', () => {
        extraFields.style.display = publishCheck.checked ? 'grid' : 'none';
    });
}

document.querySelectorAll('.edit-property').forEach(btn => btn.onclick = () => {
    pf.reset();
    editId.value = btn.dataset.propertyId;
    pf.elements.name.value = btn.dataset.name;
    pf.elements.location.value = btn.dataset.location;
    pf.elements.price.value = btn.dataset.price;
    pf.elements.status.value = btn.dataset.status;
    if (publishCheck) {
        publishCheck.checked = btn.dataset.published === '1';
        if (extraFields) extraFields.style.display = publishCheck.checked ? 'grid' : 'none';
    }
    if (pf.elements.beds && btn.dataset.beds) pf.elements.beds.value = btn.dataset.beds;
    if (pf.elements.baths && btn.dataset.baths) pf.elements.baths.value = btn.dataset.baths;
    if (pf.elements.sqft && btn.dataset.sqft) pf.elements.sqft.value = btn.dataset.sqft;
    if (pf.elements.property_type && btn.dataset.propertyType) pf.elements.property_type.value = btn.dataset.propertyType;
    if (pf.elements.listing_type) pf.elements.listing_type.value = btn.dataset.listingType === 'rent' ? 'rent' : 'sale';
    propertyDialog.querySelector('h2').textContent = 'Edit property';
    propertyDialog.showModal();
});

// Toggle PropkoNet Live status directly from property card
document.querySelectorAll('.toggle-propkonet-btn').forEach(btn => btn.onclick = async () => {
    const propId = btn.dataset.propertyId;
    const isLive = btn.dataset.published === '1';
    btn.disabled = true;
    btn.textContent = isLive ? 'Removing…' : 'Publishing…';
    try {
        const r = await fetch(`/api/property/${propId}/toggle-propkonet`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Could not update listing on PropkoNet.');
        location.reload();
    } catch (err) {
        alert(err.message || 'Failed to update PropkoNet listing.');
        btn.disabled = false;
        btn.textContent = isLive ? 'Delist PropkoNet' : 'Add to PropkoNet';
    }
});

// Handle Verification Request Submission
const vf = document.getElementById('verifyForm');
if (vf) {
    vf.addEventListener('submit', async e => {
        e.preventDefault();
        const btn = document.getElementById('submitVerifyBtn');
        const msg = document.getElementById('verifyMessage');
        const username = vf.elements.username.value.trim();
        const phone = vf.elements.phone.value.trim();
        if (!username || !phone) {
            msg.className = 'form-message is-error';
            msg.textContent = 'Please provide both your username and phone number.';
            msg.style.display = 'block';
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Submitting…';
        msg.style.display = 'none';
        try {
            const r = await fetch('/api/request-verification', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, phone })
            });
            const d = await r.json();
            if (!r.ok) {
                msg.className = 'form-message is-error';
                msg.textContent = d.error || 'Could not submit verification request.';
                msg.style.display = 'block';
                return;
            }
            msg.className = 'form-message is-success';
            msg.textContent = d.message || 'Verification request submitted successfully!';
            msg.style.display = 'block';
            setTimeout(() => {
                document.getElementById('verifyModal').close();
                location.reload();
            }, 1200);
        } catch (err) {
            msg.className = 'form-message is-error';
            msg.textContent = 'Network error. Please check your connection and try again.';
            msg.style.display = 'block';
        } finally {
            btn.disabled = false;
            btn.textContent = 'Submit verification request';
        }
    });
}
if (pf) pf.addEventListener('submit', async e => { e.preventDefault(); const id = editId.value; const submit = pf.querySelector('[type="submit"]'); const original = submit.textContent; submit.disabled = true; submit.textContent = 'Saving…'; try { const endpoint = id ? '/api/property/' + id + '/edit' : '/api/property'; const r = await fetch(endpoint, { method: 'POST', body: new FormData(pf) }); const d = await r.json(); if (!r.ok) { const message = pf.querySelector('.form-message') || document.createElement('p'); message.className = 'form-message is-error'; message.textContent = d.error || 'Could not save property.'; if (!message.parentNode) pf.insertBefore(message, submit); return } propertyDialog.close(); location.reload() } catch (err) { const message = pf.querySelector('.form-message') || document.createElement('p'); message.className = 'form-message is-error'; message.textContent = 'Could not save property. Check your connection and try again.'; if (!message.parentNode) pf.insertBefore(message, submit) } finally { submit.disabled = false; submit.textContent = original } });
let shareText = '', shareUrl = ''; document.querySelectorAll('.share-property').forEach(btn => btn.onclick = () => { shareText = btn.dataset.name + '\n' + btn.dataset.location + '\n' + btn.dataset.price + '\nStatus: ' + btn.dataset.status; shareUrl = location.origin + location.pathname + '#property-' + btn.dataset.propertyId; document.getElementById('sharePropertyText').textContent = shareText; document.getElementById('shareProperty').showModal() }); const whatsapp = document.getElementById('whatsappShare'); if (whatsapp) whatsapp.onclick = () => { navigator.clipboard?.writeText(shareText + '\n' + shareUrl); window.open('https://wa.me/?text=' + encodeURIComponent(shareText + '\n' + shareUrl), '_blank', 'noopener') }; const copy = document.getElementById('copyPropertyLink'); if (copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(shareText + '\n' + shareUrl); copy.textContent = 'Copied'; setTimeout(() => copy.textContent = 'Copy link', 1400) } catch (e) { alert('Could not copy the property details.') } };
document.querySelectorAll('.view-property').forEach(btn => btn.onclick = async () => { const dialog = document.getElementById('viewProperty'), gallery = document.getElementById('propertyGallery'); gallery.innerHTML = 'Loading photos...'; dialog.showModal(); try { const r = await fetch('/api/property/' + btn.dataset.propertyId + '/photos'); const d = await r.json(); document.getElementById('galleryTitle').textContent = d.property.name + ' photos'; gallery.innerHTML = d.photos.length ? d.photos.map(src => '<img src="' + src + '" alt="Property photo">').join('') : '<p class="gallery-empty">No photos have been added for this property.</p>' } catch (e) { gallery.innerHTML = '<p class="gallery-empty">Could not load property photos.</p>' } });
document.querySelectorAll('.delete-property').forEach(btn => btn.onclick = async () => { if (!confirm('Delete ' + btn.dataset.propertyName + '? This cannot be undone.')) return; btn.disabled = true; try { const r = await fetch('/api/property/' + btn.dataset.propertyId, { method: 'DELETE' }); const d = await r.json(); if (!r.ok) throw Error(d.error || 'Could not delete property.'); location.reload() } catch (e) { alert(e.message || 'Could not delete property.'); btn.disabled = false } });
const tf = document.getElementById('taskForm'); if (tf) tf.addEventListener('submit', async e => { e.preventDefault(); const d = Object.fromEntries(new FormData(tf).entries()); const r = await fetch('/api/task', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d) }); const result = await r.json(); if (!r.ok) return alert(result.error || 'Could not save task.'); document.getElementById('addTask').close(); location.reload() }); document.querySelectorAll('.task-toggle').forEach(btn => btn.onclick = async () => { const r = await fetch('/api/task/' + btn.dataset.taskId + '/toggle', { method: 'POST' }); if (r.ok) location.reload() });// ---------------------------------------------------------------------------
// Seller payout account (Paystack transfer recipient)
// ---------------------------------------------------------------------------
const payoutForm = document.getElementById('payoutAccountForm');
const payoutBank = document.getElementById('payoutBank');
if (payoutForm && payoutBank) {
    let banksLoaded = false;
    const dialog = document.getElementById('payoutAccountDialog');
    if (dialog) dialog.addEventListener('toggle', async () => {
        if (dialog.open && !banksLoaded) {
            banksLoaded = true;
            try {
                const res = await fetch('/api/seller/banks');
                const data = await res.json();
                if (res.ok && data.banks) {
                    payoutBank.innerHTML = '<option value="">Choose your bank</option>' +
                        data.banks.map(b => '<option value="' + b.code + '" data-name="' + b.name + '">' + b.name + '</option>').join('');
                } else {
                    payoutBank.innerHTML = '<option value="">Could not load banks</option>';
                }
            } catch (e) {
                payoutBank.innerHTML = '<option value="">Could not load banks</option>';
            }
        }
    });

    payoutForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const submit = payoutForm.querySelector('[type="submit"]');
        const msg = document.getElementById('payoutAccountMessage');
        const option = payoutBank.options[payoutBank.selectedIndex];
        const bankName = option ? option.dataset.name || option.textContent : '';
        submit.disabled = true;
        submit.textContent = 'Verifying…';
        msg.style.display = 'none';
        try {
            const res = await fetch('/api/seller/payout-account', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    bank_code: payoutBank.value,
                    bank_name: bankName,
                    account_number: document.getElementById('payoutAccountNumber').value.trim()
                })
            });
            const data = await res.json();
            if (!res.ok) {
                msg.className = 'form-message is-error';
                msg.textContent = data.error || 'Could not verify that account.';
                msg.style.display = 'block';
                return;
            }
            msg.className = 'form-message is-success';
            msg.textContent = data.message || 'Payout account saved.';
            msg.style.display = 'block';
            setTimeout(() => location.reload(), 1200);
        } catch (err) {
            msg.className = 'form-message is-error';
            msg.textContent = 'Network error. Please try again.';
            msg.style.display = 'block';
        } finally {
            submit.disabled = false;
            submit.textContent = 'Verify & save';
        }
    });
}
