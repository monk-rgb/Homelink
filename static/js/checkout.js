(function () {
    const form = document.getElementById('checkoutForm');
    if (!form) return;
    const note = document.getElementById('checkoutNote');
    const btn = document.getElementById('payNowBtn');

    form.onsubmit = async (e) => {
        e.preventDefault();
        if (btn.disabled) return;
        const fd = new FormData(form);
        const payload = Object.fromEntries(fd.entries());
        payload.property_id = form.dataset.propertyId;

        const original = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Opening secure checkout…';
        note.textContent = '';

        try {
            const res = await fetch('/api/propkonet/buy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (!res.ok) {
                note.className = 'form-note error-estimate';
                note.textContent = data.error || 'Could not start checkout. Please try again.';
                btn.disabled = false;
                btn.textContent = original;
                return;
            }
            if (data.authorization_url) {
                // Hand off to Paystack's secure hosted checkout. Success is only
                // ever confirmed server-side via the webhook / callback verify.
                window.location.assign(data.authorization_url);
            } else {
                note.className = 'form-note error-estimate';
                note.textContent = 'Could not open secure checkout. Please try again.';
                btn.disabled = false;
                btn.textContent = original;
            }
        } catch (err) {
            note.className = 'form-note error-estimate';
            note.textContent = 'Network error. Check your connection and try again.';
            btn.disabled = false;
            btn.textContent = original;
        }
    };
})();
