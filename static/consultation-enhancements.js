(() => {
  const appointmentMatch = location.pathname.match(/\/encounter\/(\d+)/);
  const followUpPane = document.querySelector('[data-pane="followup"]');
  const followUpButton = followUpPane?.querySelector('button[type="button"]');
  if (!appointmentMatch || !followUpPane || !followUpButton) return;
  followUpButton.addEventListener('click', async () => {
    const [interval, mode] = followUpPane.querySelectorAll('select');
    const reason = followUpPane.querySelector('input')?.value.trim();
    const days = Number((interval?.value.match(/\d+/) || ['30'])[0]);
    followUpButton.disabled = true;
    followUpButton.textContent = 'Scheduling…';
    try {
      const body = new URLSearchParams({ days, mode: mode?.value || 'In clinic', reason: reason || 'Clinical follow-up' });
      const response = await fetch(`/appointments/${appointmentMatch[1]}/follow-up`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
      const result = await response.json();
      if (!result.ok) throw new Error(result.message || 'Unable to schedule follow-up.');
      followUpButton.textContent = `Follow-up scheduled · ${result.scheduled_at}`;
    } catch (error) {
      alert(error.message);
      followUpButton.disabled = false;
      followUpButton.textContent = 'Confirm follow-up plan';
    }
  });
})();

(() => {
  const facts = [...document.querySelectorAll('.patient-facts > div')];
  const allergyText = facts.find(item => item.querySelector('span')?.textContent.trim() === 'Allergies')?.querySelector('b')?.textContent.trim();
  const bpText = facts.find(item => item.querySelector('span')?.textContent.trim() === 'Blood Pressure')?.querySelector('b')?.textContent.trim();
  const systolic = Number((bpText || '').match(/\d+/)?.[0]);
  const alerts = [];
  if (allergyText && !/none known|no known/i.test(allergyText)) alerts.push(`Documented allergy: ${allergyText}`);
  if (systolic >= 140) alerts.push(`Elevated systolic BP recorded: ${bpText}`);
  if (!alerts.length) return;
  const banner = document.createElement('section');
  banner.setAttribute('role', 'alert');
  banner.style.cssText = 'display:flex;gap:12px;align-items:flex-start;margin:0 0 16px;padding:14px 16px;border:1px solid #f3c083;border-left:5px solid #d97706;border-radius:10px;background:#fff8eb;color:#714313;font-size:13px;line-height:1.55';
  banner.innerHTML = `<b style="font-size:18px">!</b><div><b>Clinical safety check</b><br>${alerts.join(' · ')}<small style="display:block;margin-top:4px;color:#8a6541">Review this information before prescribing or finalising clinical decisions.</small></div>`;
  document.querySelector('.work-card')?.insertAdjacentElement('beforebegin', banner);
})();

(() => {
  const list = document.querySelector('#test-list');
  if (!list || document.getElementById('selected-tests')) return;
  const container = document.createElement('section');
  container.className = 'selected-tests'; container.hidden = true;
  container.innerHTML = '<strong>Selected laboratory tests</strong><div class="test-chips"></div>';
  list.insertAdjacentElement('afterend', container);
  const chips = container.querySelector('.test-chips');
  const update = () => {
    const selected = [...list.querySelectorAll('input[type="checkbox"]:checked')];
    chips.innerHTML = ''; container.hidden = !selected.length;
    selected.forEach(input => {
      const label = input.closest('label'); const name = label.querySelector('span').textContent;
      const chip = document.createElement('span'); chip.className = 'test-chip'; chip.append(document.createTextNode(name));
      const remove = document.createElement('button'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${name}`); remove.textContent = '×';
      remove.onclick = () => { input.checked = false; update(); }; chip.append(remove); chips.append(chip);
    });
  };
  list.addEventListener('change', update); update();
})();

(() => {
  const match = location.pathname.match(/appointments\/(\d+)\/consultation/);
  const pane = document.querySelector('[data-pane="labs"]');
  if (!match || !pane) return;
  fetch(`/api/appointments/${match[1]}/lab-reports`).then(r => r.json()).then(data => {
    const reports = data.reports || [];
    const box = document.createElement('section'); box.className = 'section-box'; box.style.marginTop = '18px';
    box.innerHTML = `<h4 class="teal">Laboratory reports for doctor review</h4>${reports.length ? reports.map(report => `<article class="rx-item" style="border-left-color:${report.status==='Finalised'?'#0f9d8a':'#f59e0b'}"><b>${report.test}</b><p>${report.ordered} · <strong>${report.status}</strong>${report.sent_to_doctor ? ' · Sent to your consultation inbox' : ''}</p>${report.parameters.length ? `<p>${report.parameters.map(p=>`${p.name}: ${p.value || '—'} ${p.unit || ''} ${p.flag && p.flag !== 'Normal' ? '['+p.flag+']':''}`).join(' · ')}</p>` : report.result ? `<p>Result: ${report.result}</p>` : ''}${report.status === 'Finalised' ? `<div style="margin-top:9px"><a class="outline-btn" href="/print/lab/${report.id}" target="_blank">Open PDF report</a> <button class="primary-btn review-lab" data-id="${report.id}" type="button">Mark reviewed</button></div>` : `<p class="save-note" style="display:inline-block">Awaiting laboratory verification</p>`}</article>`).join('') : '<p>No laboratory reports have been ordered for this patient.</p>'}`;
    pane.append(box);
    box.querySelectorAll('.review-lab').forEach(button => button.addEventListener('click', () => fetch(`/lab-orders/${button.dataset.id}/doctor-review`, {method:'POST'}).then(() => location.reload())));
  }).catch(() => {});
})();
