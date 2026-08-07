(() => {
  const resultPane = document.querySelector('[data-lab-pane="result"]');
  if (resultPane && window.LAB_ORDER_ID) {
    fetch(`/api/lab-orders/${window.LAB_ORDER_ID}/parameters`).then(r => r.json()).then(data => {
      if (!data.parameters.length) return;
      const table = resultPane.querySelector('.result-table');
      table.innerHTML = '<thead><tr><th>Parameter</th><th>Result</th><th>Unit</th><th>Reference range</th><th>Flag</th></tr></thead><tbody>' + data.parameters.map(p => `<tr data-id="${p.id}"><td><b>${p.name}</b></td><td><input value="${p.value}" aria-label="${p.name} result"></td><td>${p.unit || '—'}</td><td><span>${p.reference || 'Reference range not configured'}</span>${window.LAB_CAN_EDIT_REFERENCES ? `<button class="edit-reference" type="button" data-parameter="${p.id}" data-range="${p.reference || ''}">Edit</button>` : ''}</td><td>${p.flag || '—'}</td></tr>`).join('') + '</tbody>';
      table.querySelectorAll('.edit-reference').forEach(edit => edit.addEventListener('click', () => { const updated = prompt('Reference range for this parameter (for example: 70 - 99, Negative, or Not configured):', edit.dataset.range); if (updated === null) return; fetch(`/api/lab-parameters/${edit.dataset.parameter}/reference-range`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reference_range:updated})}).then(r => r.json()).then(result => { if (!result.ok) throw new Error(result.message || 'Unable to save'); location.reload(); }).catch(error => alert(error.message)); }));
      const button = document.createElement('button'); button.type = 'button'; button.className = 'lab-secondary'; button.textContent = 'Save parameter results';
      button.onclick = () => fetch(`/api/lab-orders/${window.LAB_ORDER_ID}/parameters`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({parameters:[...table.querySelectorAll('tr[data-id]')].map(row => ({id:row.dataset.id,value:row.querySelector('input').value}))})}).then(() => location.reload());
      resultPane.querySelector('.lab-actions')?.prepend(button);
      const checklist = document.createElement('section'); checklist.className = 'lab-ai-summary lab-completeness';
      checklist.innerHTML = '<div><b>✓ Free result completeness check</b><small>Checks entries, references and sample acceptance before verification</small></div><button type="button">Run check</button><p hidden></p>';
      resultPane.prepend(checklist);
      checklist.querySelector('button').addEventListener('click', () => {
        const checkButton = checklist.querySelector('button'); checkButton.disabled = true; checkButton.textContent = 'Checking…';
        fetch(`/api/lab-orders/${window.LAB_ORDER_ID}/completeness`).then(r => r.json()).then(data => {
          if (!data.ok) throw new Error('Unable to check');
          const message = checklist.querySelector('p'); message.hidden = false;
          message.textContent = `${data.ready ? 'Ready for laboratory verification. ' : 'Complete the listed items before verification. '}${data.checks.map(item => `${item.ok ? '✓' : '•'} ${item.label}: ${item.detail}`).join(' ')}`;
          checkButton.textContent = 'Run again'; checkButton.disabled = false;
        }).catch(() => { checkButton.textContent = 'Try again'; checkButton.disabled = false; });
      });
    });
    const workflowActions = resultPane.querySelector('.lab-actions');
    const status = document.querySelector('.lab-status-pill')?.textContent.trim();
    if (status === 'Verified') document.querySelectorAll('.lab-progress li')[3]?.classList.add('done');
    const finalise = workflowActions?.querySelector('button[value="finalise"]');
    if (finalise && status !== 'Verified') finalise.remove();
    if (workflowActions && status === 'Verification Pending') {
      const verify = document.createElement('button'); verify.type = 'submit'; verify.name = 'action'; verify.value = 'verify'; verify.className = 'lab-final'; verify.textContent = 'Verify results & release report'; workflowActions.append(verify);
    }
    if (workflowActions && status === 'Verified' && !finalise) {
      const release = document.createElement('button'); release.type = 'submit'; release.name = 'action'; release.value = 'finalise'; release.className = 'lab-final'; release.textContent = 'Finalise & release PDF'; workflowActions.append(release);
    }
    if (status === 'Finalised') {
      const quickLinks = document.querySelector('.quick-links');
      if (quickLinks && !quickLinks.querySelector('.lab-whatsapp-report')) {
        const notify = document.createElement('a');
        notify.className = 'lab-whatsapp-report';
        notify.href = `/lab-orders/${window.LAB_ORDER_ID}/whatsapp-report`;
        notify.target = '_blank';
        notify.rel = 'noopener';
        notify.textContent = '◉ WhatsApp report ready';
        quickLinks.append(notify);
      }
      const verifyBox = document.querySelector('.quick-links');
      if (verifyBox && !verifyBox.querySelector('.lab-report-verify')) {
        fetch(`/api/lab-orders/${window.LAB_ORDER_ID}/verification-link`).then(r => r.json()).then(data => {
          if (!data.ok) return;
          const verify = document.createElement('a');
          verify.className = 'lab-report-verify';
          verify.href = data.url;
          verify.target = '_blank';
          verify.rel = 'noopener';
          verify.textContent = '✓ Verify report authenticity';
          verifyBox.append(verify);
        }).catch(() => {});
      }
      const summary = document.createElement('section'); summary.className = 'lab-ai-summary';
      summary.innerHTML = '<div><b>✦ AI report summary</b><small>Finalised data only · human review required</small></div><button type="button">Generate summary</button><p hidden></p>';
      resultPane.prepend(summary);
      summary.querySelector('button').addEventListener('click', () => {
        const button = summary.querySelector('button'); button.disabled = true; button.textContent = 'Generating…';
        fetch(`/api/lab-orders/${window.LAB_ORDER_ID}/ai-summary`).then(r => r.json()).then(data => { if (!data.ok) throw new Error(data.message); const text = summary.querySelector('p'); text.hidden = false; text.textContent = `${data.summary} ${data.disclaimer}`; button.textContent = 'Regenerate summary'; button.disabled = false; }).catch(error => { alert(error.message); button.disabled = false; button.textContent = 'Generate summary'; });
      });
    }
  }
  fetch('/api/lab-patients').then(r => r.json()).then(data => {
    document.querySelectorAll('[data-lab-patients]').forEach(select => { select.innerHTML = '<option value="">Select registered patient</option>' + data.patients.map(p => `<option value="${p.id}">${p.name} · ${p.mrn}</option>`).join(''); });
    document.querySelectorAll('[data-lab-tests]').forEach(select => { select.innerHTML = '<option value="">Select test from database</option>' + data.tests.map(test => `<option value="${test}">${test}</option>`).join(''); });
  }).catch(() => {});
  document.querySelectorAll('.lab-filter-row button').forEach(button => button.addEventListener('click', () => {
    const filter = button.textContent.trim().toLowerCase();
    document.querySelectorAll('.lab-filter-row button').forEach(item => item.classList.toggle('selected', item === button));
    document.querySelectorAll('.lab-order').forEach(order => {
      const status = order.querySelector('em')?.textContent.trim().toLowerCase() || '';
      const visible = filter === 'all' || (filter === 'new' && status === 'ordered') || (filter === 'pending' && !['finalised', 'verified', 'cancelled'].includes(status));
      order.style.display = visible ? '' : 'none';
    });
  }));
  fetch('/api/lab-inventory-alerts').then(r => r.ok ? r.json() : null).then(data => {
    if (!data || !data.count) return;
    const alert = document.createElement('a'); alert.className = 'lab-stock-alert'; alert.href = '/lab-inventory';
    alert.textContent = `⚠ ${data.count} laboratory stock alert${data.count === 1 ? '' : 's'} — review inventory`;
    document.querySelector('.lab-kpis')?.insertAdjacentElement('afterend', alert);
  }).catch(() => {});
})();
