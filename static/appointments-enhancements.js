(() => {
  if (!document.querySelector('link[href*="appointment-booking-pro.css"]')) {
    const styles = document.createElement('link');
    styles.rel = 'stylesheet'; styles.href = '/static/appointment-booking-pro.css?v=20260806';
    document.head.append(styles);
  }
  document.querySelectorAll('.appointment-actions').forEach((actions) => {
    const encounter = [...actions.querySelectorAll('a')].find(link => /\/encounter\/(\d+)/.test(link.getAttribute('href') || ''));
    const match = encounter?.getAttribute('href')?.match(/\/encounter\/(\d+)/);
    if (!match || actions.querySelector('.whatsapp-reminder')) return;
    const reminder = document.createElement('a');
    reminder.className = 'whatsapp-reminder';
    reminder.href = `/appointments/${match[1]}/whatsapp-reminder`;
    reminder.target = '_blank';
    reminder.rel = 'noopener';
    reminder.textContent = 'WhatsApp';
    reminder.title = 'Open a pre-filled WhatsApp appointment reminder';
    actions.append(reminder);
    const token = document.createElement('a');
    token.href = `/print/token/${match[1]}`;
    token.target = '_blank';
    token.rel = 'noopener';
    token.textContent = 'Print token';
    token.title = 'Open printable patient queue token';
    actions.append(token);
    const status = actions.closest('tr')?.querySelector('.status')?.textContent.trim();
    if (status === 'No Show') {
      reminder.href = `/appointments/${match[1]}/whatsapp-followup`;
      reminder.textContent = 'Follow-up WhatsApp';
      reminder.title = 'Open a pre-filled follow-up reminder for a no-show patient';
    }
    if (!['Consulted', 'Cancelled', 'No Show'].includes(status)) {
      [['Cancel', 'cancel'], ['No show', 'no_show']].forEach(([label, action]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = action;
        button.textContent = label;
        button.addEventListener('click', async () => {
          const reason = prompt(`${label} reason (required):`);
          if (!reason?.trim()) return;
          const body = new URLSearchParams({ action, reason: reason.trim() });
          const response = await fetch(`/appointments/${match[1]}/attendance`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
          const result = await response.json();
          if (!result.ok) return alert(result.message || 'Unable to update this appointment.');
          location.reload();
        });
        actions.append(button);
      });
    }
  });
  const deskHead = document.querySelector('.appointment-head');
  if (deskHead && !document.querySelector('#lab-walkin-link')) {
    const link = document.createElement('a');
    link.id = 'lab-walkin-link';
    link.className = 'button';
    link.href = '/reception/lab-walkin';
    link.textContent = '⚗ Lab-only walk-in';
    deskHead.querySelector('div:last-child')?.append(link) || deskHead.append(link);
  }
  if (deskHead && !document.querySelector('#daily-queue-link')) {
    const link = document.createElement('a');
    link.id = 'daily-queue-link';
    link.className = 'button';
    link.href = '/print/daily-queue';
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Print daily queue';
    deskHead.append(link);
  }
  const form = document.querySelector('.booking-form');
  const storageKey = 'careflow-appointment-draft-v1';
  const toast = (message, tone = 'info') => {
    let node = document.querySelector('#appointment-live-notice');
    if (!node) { node = document.createElement('div'); node.id = 'appointment-live-notice'; node.setAttribute('role', 'status'); document.body.append(node); }
    node.textContent = message; node.dataset.tone = tone; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 4200);
  };
  const bookingConfirmation = document.querySelector('.flash.success')?.textContent.trim();
  if (bookingConfirmation && /appointment registered|appointment scheduled/i.test(bookingConfirmation)) {
    setTimeout(() => toast(bookingConfirmation, 'success'), 120);
  }
  if (form) {
    const bookingCard = form.closest('.booking-card');
    const progress = document.createElement('div');
    progress.className = 'booking-progress';
    progress.innerHTML = '<div class="active"><b>1</b>Choose patient</div><div><b>2</b>Visit details</div><div><b>3</b>Confirm booking</div>';
    bookingCard?.querySelector('.booking-title')?.after(progress);
    const icons = { existing: '⌕', new: '+', followup: '↻' };
    document.querySelectorAll('[data-booking]').forEach(button => {
      if (!button.querySelector('.booking-tab-icon')) button.insertAdjacentHTML('afterbegin', `<i class="booking-tab-icon">${icons[button.dataset.booking] || '•'}</i>`);
    });
    [['.booking-existing', '1. Find patient', 'Choose a registered patient by name, UHID or mobile number.'], ['.booking-new', '1. Patient profile', 'Registration and appointment booking are completed together.'], ['.booking-followup', '1. Follow-up patient', 'Select the patient and suggested review interval.']].forEach(([selector, title, detail]) => {
      const section = form.querySelector(selector);
      if (section && !section.querySelector('.booking-section-heading')) section.insertAdjacentHTML('afterbegin', `<div class="booking-section-heading"><b>${title}</b><span>${detail}</span></div>`);
    });
    const visitDetails = document.createElement('section');
    visitDetails.className = 'booking-visit-details';
    const submit = form.querySelector('button[type="submit"], button:not([type])');
    const visitFields = [...form.children].filter(child => child.matches?.('label') && !child.classList.contains('full'));
    const reason = [...form.children].find(child => child.matches?.('label.full') && child.querySelector('[name="reason"]'));
    visitFields.forEach(field => visitDetails.append(field));
    if (reason) visitDetails.append(reason);
    if (submit) form.insertBefore(visitDetails, submit); else form.append(visitDetails);
    const setProgress = type => {
      const first = progress.querySelector('div:first-child');
      if (first) first.querySelector('span')?.remove();
      progress.querySelectorAll('div').forEach((step, index) => step.classList.toggle('active', index === 0 || type !== 'existing' && index === 1));
    };
    document.querySelectorAll('[data-booking]').forEach(button => button.addEventListener('click', () => setProgress(button.dataset.booking)));
    const saveDraft = () => {
      const values = Object.fromEntries(new FormData(form).entries());
      localStorage.setItem(storageKey, JSON.stringify(values));
    };
    const draft = localStorage.getItem(storageKey);
    if (draft && confirm('Restore your unfinished appointment booking?')) {
      Object.entries(JSON.parse(draft)).forEach(([name, value]) => { const field = form.elements.namedItem(name); if (field && 'value' in field) field.value = value; });
      toast('Offline booking draft restored.');
    }
    form.addEventListener('input', saveDraft);
    form.addEventListener('submit', event => {
      if (!navigator.onLine) { event.preventDefault(); saveDraft(); toast('You are offline. The booking draft is safely saved and can be submitted after reconnecting.', 'warning'); return; }
      localStorage.removeItem(storageKey);
    });
  }
  document.addEventListener('keydown', event => {
    if (event.target.matches('input,select,textarea')) return;
    if (event.key.toLowerCase() === 'n') { event.preventDefault(); document.querySelector('#new-appointment')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    if (event.key.toLowerCase() === 'c') location.href = '/appointments/calendar';
    if (event.key.toLowerCase() === 't') window.open('/appointments/token-display', '_blank');
    if (event.key === '?') toast('Keyboard shortcuts: N new appointment · C calendar · T token display');
  });
  let lastSignature = '';
  const poll = async () => {
    if (!navigator.onLine) return;
    try { const response = await fetch('/api/appointments/live', { headers: { Accept: 'application/json' } }); const data = await response.json(); const signature = JSON.stringify(data.queue); if (lastSignature && signature !== lastSignature) toast('Live queue updated. Refreshing status shortly.', 'success'); lastSignature = signature; } catch (_) { /* network temporarily unavailable */ }
  };
  poll(); setInterval(poll, 15000);
  window.addEventListener('offline', () => toast('Offline mode: booking drafts will be saved on this device.', 'warning'));
  window.addEventListener('online', () => toast('Connection restored. You can submit saved appointment drafts.', 'success'));
})();
