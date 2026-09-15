(function () {
    const profileForm = document.getElementById('profileForm');
    if (profileForm) {
        profileForm.onsubmit = async (e) => {
            e.preventDefault();
            const note = document.getElementById('profileNote');
            const fd = new FormData(profileForm);
            const payload = Object.fromEntries(fd.entries());
            payload.available = profileForm.querySelector('[name=available]').checked ? '1' : '0';
            note.textContent = 'Saving…';
            try {
                const r = await fetch('/api/handyman/profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Could not save profile');
                note.textContent = 'Saved. Your profile is live in the directory.';
                setTimeout(() => location.reload(), 700);
            } catch (err) {
                note.textContent = err.message;
            }
        };
    }

    const jobForm = document.getElementById('jobForm');
    if (jobForm) {
        jobForm.onsubmit = async (e) => {
            e.preventDefault();
            const note = document.getElementById('jobNote');
            const fd = new FormData(jobForm);
            note.textContent = 'Recording…';
            try {
                const r = await fetch('/api/handyman/jobs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(Object.fromEntries(fd.entries()))
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Could not add job');
                note.textContent = 'Job recorded. Links updated to ' + d.profile.links + '/10.';
                setTimeout(() => location.reload(), 800);
            } catch (err) {
                note.textContent = err.message;
            }
        };
    }
})();
