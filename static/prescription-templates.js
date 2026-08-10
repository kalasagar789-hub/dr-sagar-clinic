(() => {
  const consultPage = document.querySelector('.consult-page');
  if (consultPage && !document.querySelector('.consult-live-queue')) {
    const panel = document.createElement('section');
    panel.className = 'consult-live-queue';
    panel.innerHTML = '<div class="consult-queue-head"><div><small>DOCTOR WORKSPACE</small><h3>Today\'s queue</h3></div><span class="queue-count">0</span></div><p>Select a patient to open their consultation.</p><div class="consult-queue-list"><div class="queue-loading">Loading patients…</div></div><a href="/appointments" class="queue-link">Open appointment queue →</a>';
    consultPage.insertBefore(panel, consultPage.firstChild);
    const list = panel.querySelector('.consult-queue-list');
    const currentId = Number(location.pathname.split('/').pop());
    const statusClass = value => String(value).toLowerCase().replaceAll(' ', '-');
    const loadQueue = () => fetch('/api/consultation-queue').then(response => response.ok ? response.json() : Promise.reject()).then(data => {
      panel.querySelector('.queue-count').textContent = data.queue.length;
      list.innerHTML = data.queue.length ? data.queue.map(item => `<a href="/encounter/${item.id}" class="consult-queue-row ${item.id === currentId ? 'active' : ''}"><span class="queue-initial">${item.initial}</span><span><b>${item.patient}</b><small>Token ${item.id} · ${item.time}</small><em class="${statusClass(item.status)}">${item.status}</em></span></a>`).join('') : '<div class="queue-empty">No patients waiting for consultation.</div>';
    }).catch(() => { list.innerHTML = '<div class="queue-empty">Unable to load the consultation queue.</div>'; });
    loadQueue(); setInterval(loadQueue, 30000);
  }
  const banner = document.querySelector('.patient-banner');
  if (banner && !document.querySelector('.consult-patient-trigger')) {
    const trigger = document.createElement('button');
    trigger.type = 'button'; trigger.className = 'consult-patient-trigger';
    trigger.innerHTML = 'Patients <span>0</span>';
    const modal = document.createElement('div');
    modal.className = 'patient-switcher-modal'; modal.hidden = true;
    modal.innerHTML = '<section class="patient-switcher" role="dialog" aria-modal="true" aria-label="Select patient"><div class="switcher-head"><div><small>CONSULTATION QUEUE</small><h3>Switch patient</h3></div><button type="button" aria-label="Close patient list">×</button></div><input type="search" placeholder="Search patient name, token or status" aria-label="Search patients"><div class="switcher-list"><p>Loading patients…</p></div></section>';
    banner.append(trigger); document.body.append(modal);
    const list = modal.querySelector('.switcher-list'); const searchPatients = modal.querySelector('input');
    let queue = [];
    const render = () => { const query = searchPatients.value.trim().toLowerCase(); const items = queue.filter(item => `${item.patient} ${item.id} ${item.status}`.toLowerCase().includes(query)); list.innerHTML = items.length ? items.map(item => `<a href="/encounter/${item.id}"><span>${item.initial}</span><div><b>${item.patient}</b><small>Token ${item.id} · ${item.time} · ${item.status}</small></div><i>Open</i></a>`).join('') : '<p>No matching patient found.</p>'; };
    fetch('/api/consultation-queue').then(response => response.json()).then(data => { queue = data.queue; trigger.querySelector('span').textContent = queue.length; render(); }).catch(() => { list.innerHTML = '<p>Unable to load patients right now.</p>'; });
    trigger.addEventListener('click', () => { modal.hidden = false; searchPatients.focus(); });
    modal.querySelector('.switcher-head button').addEventListener('click', () => { modal.hidden = true; });
    modal.addEventListener('click', event => { if (event.target === modal) modal.hidden = true; });
    searchPatients.addEventListener('input', render);
  }
  const summaryPane = document.querySelector('[data-pane="summary"]');
  const clinicalForm = document.querySelector('#clinical-form');
  if (summaryPane && clinicalForm && !document.querySelector('.clinical-assist')) {
    const assist = document.createElement('section');
    assist.className = 'clinical-assist';
    assist.innerHTML = '<div class="assist-title"><div><small>FREE CLINICAL ASSISTANT</small><h4>Notes, prescription & knowledge help</h4></div><span>Doctor review required</span></div><p>Create editable documentation drafts and open focused clinical-reference prompts. It never issues a diagnosis or medication dose.</p><div class="assist-actions"><button type="button" data-assist="summary">Create summary</button><button type="button" data-assist="advice">Advice & follow-up</button><button type="button" data-assist="rx-notes">Prescription notes</button><button type="button" data-assist="knowledge">Clinical knowledge</button><button type="button" data-assist="templates">Template ideas</button></div><div class="assist-result" aria-live="polite">Choose an option to create a clinician-reviewable draft.</div>';
    const summaryHeading = summaryPane.querySelector('.pane-head');
    if (summaryHeading) summaryHeading.insertAdjacentElement('afterend', assist);
    else summaryPane.prepend(assist);
    const result = assist.querySelector('.assist-result');
    const requestHeaders = () => ({'Content-Type':'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''});
    const visitText = () => ({history: clinicalForm.querySelector('[name="history"]')?.value || '', diagnosis: clinicalForm.querySelector('[name="diagnosis"]')?.value || '', notes: clinicalForm.querySelector('[name="notes"]')?.value || ''});
    const postAssistant = (url) => fetch(url, {method:'POST', headers:requestHeaders(), body:JSON.stringify(visitText())}).then(async response => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || (response.status === 403 ? 'Your session has expired. Refresh the page and sign in again.' : 'The assistant is temporarily unavailable.'));
      return data;
    });
    const requestDraft = () => postAssistant('/api/consultation-assist');
    const requestKnowledge = () => postAssistant('/api/clinical-knowledge');
    assist.querySelectorAll('[data-assist]').forEach(button => button.addEventListener('click', () => {
      result.innerHTML = '<p>Preparing a draft…</p>';
      const type = button.dataset.assist;
      const action = type === 'knowledge' ? requestKnowledge() : requestDraft();
      assist.querySelectorAll('[data-assist]').forEach(item => { item.disabled = true; });
      action.then(data => {
        if (type === 'summary') result.innerHTML = `<b>Visit summary draft</b><p>${data.summary}</p><button type="button" class="apply-summary">Add to notes</button>`;
        if (type === 'advice') result.innerHTML = `<b>Advice & follow-up draft</b><ul>${data.advice.map(item => `<li>${item}</li>`).join('')}</ul><strong>${data.follow_up}</strong>`;
        if (type === 'rx-notes') result.innerHTML = `<b>Prescription counselling notes</b><ul>${data.prescription_notes.map(item => `<li>${item}</li>`).join('')}</ul><button type="button" class="apply-rx-notes">Add to notes</button>`;
        if (type === 'knowledge') result.innerHTML = `<b>Clinical knowledge: ${data.topic}</b><p class="knowledge-disclaimer">${data.disclaimer}</p><div class="knowledge-list">${data.items.map(item => `<article><strong>${item.title}</strong><p>${item.detail}</p></article>`).join('')}</div>`;
        if (type === 'templates') result.innerHTML = `<b>Relevant prescription templates</b><div class="assist-template-list">${data.templates.length ? data.templates.map(item => `<span>${item.category}: ${item.name}</span>`).join('') : '<span>Review the full template catalogue in Prescription.</span>'}</div>`;
        const apply = result.querySelector('.apply-summary');
        if (apply) apply.addEventListener('click', () => { const notes = clinicalForm.querySelector('[name="notes"]'); notes.value = `${notes.value ? notes.value + '\n\n' : ''}${data.summary}`; notes.focus(); });
        const applyPrescriptionNotes = result.querySelector('.apply-rx-notes');
        if (applyPrescriptionNotes) applyPrescriptionNotes.addEventListener('click', () => { const notes = clinicalForm.querySelector('[name="notes"]'); notes.value = `${notes.value ? notes.value + '\n\n' : ''}${data.prescription_notes.join('\n')}`; notes.focus(); });
      }).catch(error => { result.textContent = error.message || 'The assistant draft could not be prepared. Please continue documenting manually.'; }).finally(() => {
        assist.querySelectorAll('[data-assist]').forEach(item => { item.disabled = false; });
      });
    }));
  }
  if (banner && !document.querySelector('.consult-ai-trigger')) {
    const aiTrigger = document.createElement('button');
    aiTrigger.type = 'button'; aiTrigger.className = 'consult-ai-trigger'; aiTrigger.textContent = '✦ AI Assist';
    aiTrigger.addEventListener('click', () => { document.querySelector('[data-tab="summary"]')?.click(); setTimeout(() => document.querySelector('.clinical-assist')?.scrollIntoView({behavior:'smooth', block:'center'}), 40); });
    banner.append(aiTrigger);
  }
  const master = document.querySelector('#medicine-master');
  const search = document.querySelector('#medicine-filter');
  const templates = window.CLINIC_RX_TEMPLATES || [];
  if (!master || !search) return;
  const form = master.closest('form');
  if (!form) return;

  const prescriptionPane = form.closest('[data-pane="prescription"]');
  const notify = (message, tone = 'success') => {
    let toast = document.querySelector('.clinic-action-toast');
    if (!toast) {
      toast = document.createElement('div'); toast.className = 'clinic-action-toast';
      toast.setAttribute('role', 'status'); toast.setAttribute('aria-live', 'polite'); document.body.append(toast);
    }
    toast.textContent = message; toast.dataset.tone = tone; toast.classList.add('show');
    clearTimeout(window.clinicActionToastTimer);
    window.clinicActionToastTimer = setTimeout(() => toast.classList.remove('show'), 4500);
  };
  const serverFlash = document.querySelector('.flash.success, .flash.warning, .flash.danger');
  if (serverFlash) setTimeout(() => notify(serverFlash.textContent.trim(), serverFlash.classList.contains('danger') ? 'danger' : serverFlash.classList.contains('warning') ? 'warning' : 'success'), 120);
  if (prescriptionPane && !prescriptionPane.querySelector('.rx-fullscreen-toggle')) {
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'rx-fullscreen-toggle';
    const setFullScreen = enabled => {
      prescriptionPane.classList.toggle('rx-fullscreen', enabled); document.body.classList.toggle('rx-screen-open', enabled);
      toggle.textContent = enabled ? 'Exit full screen' : 'Open full-screen prescription';
      if (enabled) search.focus({ preventScroll: true });
    };
    toggle.addEventListener('click', () => setFullScreen(!prescriptionPane.classList.contains('rx-fullscreen')));
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && prescriptionPane.classList.contains('rx-fullscreen')) setFullScreen(false); });
    prescriptionPane.querySelector('.pane-head')?.append(toggle); setFullScreen(false);
  }

  const box = document.createElement('section');
  box.className = 'rx-template-box';
  box.innerHTML = '<div class="rx-template-heading"><span>Quick prescription templates</span><small>Select once to add a safe starting plan</small></div><label>Suggested template<select name="template_id"><option value="">Choose a template (optional)</option></select></label><div class="rx-template-suggestions"></div><div class="rx-template-preview">Choose a template to preview medicines, combinations and advice.</div>';
  const select = box.querySelector('select');
  const preview = box.querySelector('.rx-template-preview');
  const suggestions = box.querySelector('.rx-template-suggestions');
  function showTemplate(template) {
    if (!template) { preview.textContent = 'Choose a template to preview medicines, combinations and advice.'; return; }
    const names = template.items.split('\n').map(item => item.split('|')[0]).join(' · ');
    preview.innerHTML = `<b>${template.name}</b><span>${names}</span><small>${template.advice || 'Review and edit before signing.'}</small>`;
  }
  templates.forEach((template, index) => {
    const option = document.createElement('option');
    option.value = template.id;
    option.textContent = `${template.category} — ${template.name}`;
    select.append(option);
    if (index < 4) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = template.name; button.title = `Use ${template.name}`;
      button.addEventListener('click', () => { select.value = String(template.id); showTemplate(template); });
      suggestions.append(button);
    }
  });
  select.addEventListener('change', () => showTemplate(templates.find(item => String(item.id) === select.value)));
  form.insertBefore(box, form.firstChild);

  const dosageInput = form.querySelector('input[name="dosage"]');
  if (dosageInput) {
    const schedule = document.createElement('select');
    schedule.name = 'dosage'; schedule.className = 'dose-schedule';
    schedule.setAttribute('aria-label', 'Dose and timing schedule');
    const schedules = ['1-0-0 · Morning', '0-1-0 · Afternoon', '0-0-1 · Night', '1-0-1 · Morning & night', '1-1-1 · Three times daily', '0-1-1 · Afternoon & night', '1-1-0 · Morning & afternoon', 'Once weekly', 'SOS / when required', 'As directed'];
    schedule.innerHTML = '<option value="">Dose & timing</option>' + schedules.map(item => `<option value="${item.split(' · ')[0]}">${item}</option>`).join('');
    dosageInput.replaceWith(schedule);
  }

  // Build one prescription with many medicines before sending it to pharmacy.
  const dosageControl = form.querySelector('[name="dosage"]');
  const durationControl = form.querySelector('[name="duration"]');
  const quantityControl = form.querySelector('[name="quantity"]');
  const instructionsControl = form.querySelector('[name="instructions"]');
  const submitPrescription = form.querySelector('button.primary-btn');
  if (dosageControl && durationControl && quantityControl && instructionsControl && submitPrescription && !form.querySelector('.rx-cart')) {
    instructionsControl.hidden = false; instructionsControl.type = 'text'; instructionsControl.placeholder = 'Instruction, e.g. After food';
    [master, dosageControl, durationControl, quantityControl, instructionsControl].forEach(field => field.removeAttribute('name'));
    const fieldLabels = [
      [master, 'Medicine', 'medicine'], [dosageControl, 'Dose & timing', 'dose'],
      [durationControl, 'Duration', 'duration'], [quantityControl, 'Quantity', 'quantity'],
      [instructionsControl, 'Instructions', 'instructions'],
    ];
    fieldLabels.forEach(([control, label, className]) => {
      if (control.closest('.rx-field')) return;
      const wrapper = document.createElement('label'); wrapper.className = `rx-field rx-field-${className}`;
      const caption = document.createElement('span'); caption.textContent = label;
      control.parentNode.insertBefore(wrapper, control); wrapper.append(caption, control);
    });
    submitPrescription.textContent = 'Create prescription & preview';
    const cart = document.createElement('section');
    cart.className = 'rx-cart';
    cart.innerHTML = '<div class="rx-cart-head"><div><small>PRESCRIPTION MEDICINES</small><h4>Medicine list <span>0</span></h4></div><p>Add one or more medicines, then review before creating the prescription.</p></div><div class="rx-cart-items"></div><p class="rx-cart-empty">No medicines added yet.</p>';
    const items = cart.querySelector('.rx-cart-items'); const empty = cart.querySelector('.rx-cart-empty'); const count = cart.querySelector('h4 span');
    const add = document.createElement('button'); add.type = 'button'; add.className = 'rx-add-medicine'; add.textContent = '+ Add medicine to prescription';
    submitPrescription.insertAdjacentElement('beforebegin', cart); cart.insertAdjacentElement('beforebegin', add);
    const updateCart = () => { const rows = items.querySelectorAll('.rx-cart-item'); count.textContent = rows.length; empty.hidden = Boolean(rows.length); };
    const field = (name, value) => { const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; return input; };
    const addMedicine = () => {
      const option = master.options[master.selectedIndex];
      if (!master.value) return alert('Select a medicine from the pharmacy master first.');
      const dose = dosageControl.value || 'As directed'; const duration = durationControl.value.trim() || 'As directed';
      const quantity = Math.max(1, Number(quantityControl.value || 1)); const instructions = instructionsControl.value.trim() || 'As directed';
      const row = document.createElement('article'); row.className = 'rx-cart-item';
      const label = option.textContent.split(' â€” ')[0];
      row.innerHTML = `<div><b>${label}</b><small>${dose} · ${duration} · Qty ${quantity} · ${instructions}</small></div><span><button type="button" class="rx-edit">Edit</button><button type="button" class="rx-remove">Remove</button></span>`;
      row.append(field('medicine_id', master.value), field('dosage', dose), field('duration', duration), field('quantity', quantity), field('instructions', instructions));
      row.querySelector('.rx-remove').addEventListener('click', () => { row.remove(); updateCart(); });
      row.querySelector('.rx-edit').addEventListener('click', () => {
        master.value = row.querySelector('[name="medicine_id"]').value; dosageControl.value = row.querySelector('[name="dosage"]').value;
        durationControl.value = row.querySelector('[name="duration"]').value; quantityControl.value = row.querySelector('[name="quantity"]').value;
        instructionsControl.value = row.querySelector('[name="instructions"]').value; search.value = master.options[master.selectedIndex].textContent.split(' â€” ')[0];
        row.remove(); updateCart(); master.focus();
      });
      items.append(row); select.value = ''; master.value = ''; search.value = ''; dosageControl.value = ''; durationControl.value = ''; quantityControl.value = 30; instructionsControl.value = 'After food'; updateCart(); notify(`${label} added to prescription.`); search.focus({ preventScroll: true });
    };
    add.addEventListener('click', addMedicine);
    form.addEventListener('submit', event => { if (!items.children.length && !select.value) { event.preventDefault(); alert('Add one or more medicines, or select a prescription template.'); } });
    const style = document.createElement('style'); style.id = 'rx-cart-style';
    style.textContent = '.rx-add-medicine{margin:12px 0 0;border:1px dashed #26a69a;background:#effbf8;color:#087e72;border-radius:9px;padding:10px 12px;font-size:13px;font-weight:800;cursor:pointer}.rx-add-medicine:hover{background:#087e72;color:#fff}.rx-cart{margin:14px 0 0;border:1px solid #cbe8e2;border-radius:12px;overflow:hidden;background:#fbfffe}.rx-cart-head{display:flex;justify-content:space-between;gap:12px;align-items:start;padding:12px 14px;background:linear-gradient(110deg,#effbf8,#f1f7ff);border-bottom:1px solid #dbeeea}.rx-cart-head small{color:#138779;font-size:9px;font-weight:800;letter-spacing:.6px}.rx-cart-head h4{margin:3px 0 0;color:#183e5a;font-size:15px}.rx-cart-head h4 span{display:inline-grid;place-items:center;min-width:20px;height:20px;border-radius:10px;background:#1565c0;color:#fff;font-size:10px}.rx-cart-head p{margin:2px 0 0;max-width:230px;color:#678092;font-size:11px;line-height:1.4}.rx-cart-items{display:grid;gap:8px;padding:10px}.rx-cart-item{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px;border:1px solid #dce9ed;border-left:4px solid #26a69a;border-radius:8px;background:#fff}.rx-cart-item b,.rx-cart-item small{display:block}.rx-cart-item b{color:#173d5b;font-size:13px}.rx-cart-item small{margin-top:4px;color:#638094;font-size:11px}.rx-cart-item span{display:flex;gap:6px}.rx-cart-item button{border:1px solid #cbdde5;border-radius:6px;background:#fff;color:#1765a9;padding:6px 8px;font-size:10px;font-weight:800;cursor:pointer}.rx-cart-item .rx-remove{color:#b54747;border-color:#f0cccc}.rx-cart-empty{margin:0;padding:14px;color:#70889a;font-size:12px;text-align:center}@media(max-width:560px){.rx-cart-head,.rx-cart-item{display:block}.rx-cart-item span{margin-top:9px}.rx-cart-head p{margin-top:7px}}';
    if (!document.getElementById(style.id)) document.head.append(style);
  }

  const results = document.createElement('div');
  results.className = 'rx-drug-results'; results.setAttribute('role', 'listbox'); results.hidden = true;
  search.insertAdjacentElement('afterend', results);
  const medicines = [...master.options].slice(1);
  function renderResults() {
    const query = search.value.trim().toLowerCase();
    if (!query) { results.hidden = true; results.innerHTML = ''; return; }
    const matches = medicines.filter(option => (option.dataset.search || option.textContent.toLowerCase()).includes(query));
    results.innerHTML = '';
    if (!matches.length) results.innerHTML = '<p>No medicine or combination found. Try a brand, generic, or composition.</p>';
    matches.forEach(option => {
      const button = document.createElement('button');
      button.type = 'button'; button.setAttribute('role', 'option');
      const [name, detail] = option.textContent.split(' — ');
      button.innerHTML = `<b>${name}</b><small>${detail || 'Medicine database'}</small>`;
      button.addEventListener('click', () => { master.value = option.value; search.value = name; results.hidden = true; master.dispatchEvent(new Event('change', { bubbles: true })); });
      results.append(button);
    });
    results.hidden = false;
  }
  search.addEventListener('input', renderResults);
  search.addEventListener('focus', renderResults);
  document.addEventListener('click', event => { if (!results.contains(event.target) && event.target !== search) results.hidden = true; });
})();
