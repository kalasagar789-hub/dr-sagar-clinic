(() => {
  const token = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const selectedHeader = document.querySelector('.lab-order-header');
  if (!selectedHeader) return;
  const patientId = new URLSearchParams(location.search).get('patient_id') || window.LAB_SELECTED_PATIENT_ID;
  const selectedOrderId = window.LAB_ORDER_ID;
  if (!patientId && !selectedOrderId) return;

  const launch = document.querySelector('.lab-v2-result-action .bulk-results-launch') || document.createElement('button');
  if (!launch.isConnected) {
    launch.type = 'button'; launch.className = 'bulk-results-launch'; launch.textContent = 'Enter all patient results';
    selectedHeader.append(launch);
  }
  const modal = document.createElement('section'); modal.className = 'bulk-modal'; modal.hidden = true;
  modal.innerHTML = '<div class="bulk-dialog" role="dialog" aria-modal="true" aria-labelledby="bulk-results-title"><header class="bulk-head"><div><h2 id="bulk-results-title">Result entry</h2><p>Loading patient worklist…</p></div><button class="bulk-close" type="button" aria-label="Close result entry">×</button></header><main class="bulk-body"></main><footer class="bulk-footer"><span class="bulk-status">Enter results, then save a draft or submit all completed tests.</span><button type="button" class="bulk-save">Save draft</button><button type="button" class="bulk-submit">Submit for verification</button></footer></div>';
  document.body.append(modal);
  const close = () => { modal.hidden = true; document.body.style.overflow = ''; };
  modal.querySelector('.bulk-close').onclick = close;
  modal.addEventListener('click', event => { if (event.target === modal) close(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !modal.hidden) close(); });

  let worklist = null;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
  const render = data => {
    worklist = data;
    modal.querySelector('.bulk-head p').textContent = `${data.patient.name} · ${data.patient.mrn} · ${data.orders.length} unfinished test(s)`;
    const body = modal.querySelector('.bulk-body');
    body.innerHTML = '<div class="bulk-notice">Enter all tests for this patient in one place. Reference ranges are shown from the laboratory master and results remain drafts until submitted.</div>' + data.orders.map(order => {
      const rows = order.parameters.length ? order.parameters.map(parameter => `<div class="bulk-grid" data-parameter="${parameter.id}"><label>${escape(parameter.name)}<input value="${escape(parameter.value)}" inputmode="decimal" aria-label="${escape(parameter.name)} result"></label><div><b class="bulk-reference">${escape(parameter.reference)}</b></div><div class="bulk-unit">${escape(parameter.unit || '—')}</div><div class="bulk-unit">${escape(parameter.flag || '—')}</div></div>`).join('') : `<div class="bulk-grid bulk-single"><label>Result<input class="bulk-generic-result" value="${escape(order.result_value)}" aria-label="${escape(order.test_name)} result"></label><div><b class="bulk-reference">Reference range</b><input class="bulk-generic-reference" value="${escape(order.reference_range)}" placeholder="Enter reference range"></div></div>`;
      return `<article class="bulk-test" data-order="${order.id}"><header class="bulk-test-heading"><h3>${escape(order.test_name)}</h3><span class="${order.sample_ready ? '' : 'sample-warning'}">${order.sample_ready ? escape(order.status) : 'Sample collection required'}</span></header>${rows}<label class="bulk-comment">Laboratory comment<textarea>${escape(order.remarks)}</textarea></label></article>`;
    }).join('');
  };
  const collect = () => ({orders: [...modal.querySelectorAll('.bulk-test')].map(card => ({id: Number(card.dataset.order), parameters: [...card.querySelectorAll('[data-parameter]')].map(row => ({id: Number(row.dataset.parameter), value: row.querySelector('input').value})), result_value: card.querySelector('.bulk-generic-result')?.value || '', reference_range: card.querySelector('.bulk-generic-reference')?.value || '', remarks: card.querySelector('textarea')?.value || ''}))});
  const save = async action => {
    const buttons = modal.querySelectorAll('.bulk-footer button'); buttons.forEach(button => button.disabled = true);
    const status = modal.querySelector('.bulk-status'); status.textContent = action === 'submit' ? 'Checking and submitting all results…' : 'Saving result draft…';
    try {
      const response = await fetch(`/api/lab-patients/${worklist.patient.id}/result-worklist`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token()}, body: JSON.stringify({...collect(), action})});
      const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || 'Unable to save results.');
      status.textContent = data.message; setTimeout(() => location.reload(), 700);
    } catch (error) { status.textContent = error.message; buttons.forEach(button => button.disabled = false); }
  };
  modal.querySelector('.bulk-save').onclick = () => save('save');
  modal.querySelector('.bulk-submit').onclick = () => save('submit');
  launch.onclick = async () => {
    modal.hidden = false; document.body.style.overflow = 'hidden';
    try {
      let url = patientId ? `/api/lab-patients/${patientId}/result-worklist` : null;
      if (!url) {
        const patientLink = selectedHeader.querySelector('a[data-patient-id]');
        url = patientLink ? `/api/lab-patients/${patientLink.dataset.patientId}/result-worklist` : null;
      }
      if (!url) throw new Error('Patient worklist is unavailable. Refresh and select the test again.');
      const response = await fetch(url); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || 'Unable to load patient tests.'); render(data);
    } catch (error) { modal.querySelector('.bulk-body').innerHTML = `<div class="bulk-notice">${escape(error.message)}</div>`; }
  };
})();
