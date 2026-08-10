document.addEventListener('DOMContentLoaded', () => {
  const booking = document.querySelector('.booking-card');
  const head = document.querySelector('.appointment-head');
  if (!booking || !head) return;
  head.insertAdjacentElement('afterend', booking);
  const flow = document.createElement('section');
  flow.className = 'reception-flow';
  flow.setAttribute('aria-label', 'Reception workflow');
  flow.innerHTML = `
    <div class="now"><b>1 · Register / book</b><small>New, existing, follow-up or lab-only</small></div>
    <div><b>2 · Check in</b><small>Token is assigned automatically</small></div>
    <div><b>3 · Record vitals</b><small>Reception sends patient to waiting queue</small></div>
    <div><b>4 · Doctor consults</b><small>Orders and prescription are created</small></div>
    <div><b>5 · Lab / Pharmacy</b><small>Complete requested services</small></div>`;
  booking.insertAdjacentElement('afterend', flow);

  document.querySelectorAll('.appointment-queue tr').forEach((row) => {
    const status = row.querySelector('.status')?.textContent.trim();
    const encounterUrl = row.querySelector('.appointment-actions a[href*="/encounter/"]')?.getAttribute('href');
    const id = encounterUrl?.match(/\/encounter\/(\d+)/)?.[1];
    const actions = row.querySelector('.appointment-actions');
    if (status === 'Vitals Pending' && id && actions && ['admin', 'reception'].includes(document.body.dataset.role)) {
      const vitals = document.createElement('a');
      vitals.href = `/appointments/${id}/vitals`;
      vitals.textContent = 'Record vitals';
      vitals.className = 'record-vitals';
      actions.append(vitals);
    }
  });
});
