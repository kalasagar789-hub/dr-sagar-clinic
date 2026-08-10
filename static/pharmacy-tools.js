(() => {
  const pharmacyHero = document.querySelector('.pharm-hero > div:last-child');
  if (pharmacyHero && !pharmacyHero.querySelector('.collections-link')) {
    const link = document.createElement('a'); link.className = 'collections-link'; link.href = '/billing/collections'; link.textContent = 'Payments & collections'; pharmacyHero.append(link);
  }
  document.querySelectorAll('.pharm-hero a[href="#add-batch"], .pharm-hero a[href="#new-medicine"]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); location.href = `/pharmacy?tab=stock${link.getAttribute('href')}`; }));
  const form = document.querySelector('form.pos-grid');
  if (!form) return;
  const patient = form.querySelector('select[name="patient_id"]');
  const medicine = form.querySelector('select[name="medicine_id"]');
  const quantity = form.querySelector('input[name="quantity"]');
  if (!patient || !medicine || !quantity) return;
  const prescriptions = window.PHARMACY_PATIENT_PRESCRIPTIONS || {};
  const opened = window.PHARMACY_RX;
  if (opened) patient.value = String(opened.patient_id);

  const medicineLabel = medicine.closest('label');
  const quantityLabel = quantity.closest('label');
  const rxPanel = document.createElement('section'); rxPanel.className = 'pos-prescription-panel';
  const manualNotice = document.createElement('div'); manualNotice.className = 'pos-manual-notice';
  const hiddenLines = document.createElement('div'); hiddenLines.className = 'pos-rx-hidden-lines';
  form.insertBefore(rxPanel, medicineLabel);
  form.insertBefore(manualNotice, medicineLabel);
  form.append(hiddenLines);

  const prescriptionInput = document.createElement('input'); prescriptionInput.type = 'hidden'; prescriptionInput.name = 'prescription_id'; form.append(prescriptionInput);
  const money = value => `₹${Number(value || 0).toFixed(2)}`;
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));

  const showPatientPrescription = () => {
    const rx = prescriptions[String(patient.value)];
    hiddenLines.innerHTML = '';
    if (!rx) {
      prescriptionInput.value = '';
      rxPanel.hidden = true; manualNotice.hidden = false;
      manualNotice.innerHTML = '<b>No pending doctor prescription</b><span>This patient has no pending prescription. Use manual medicine entry only for a walk-in counter sale.</span>';
      medicineLabel.hidden = false; quantityLabel.hidden = false;
      medicine.disabled = false; quantity.disabled = false;
      return;
    }
    prescriptionInput.value = String(rx.id);
    medicineLabel.hidden = true; quantityLabel.hidden = true; medicine.disabled = true; quantity.disabled = true; manualNotice.hidden = true; rxPanel.hidden = false;
    let total = 0;
    const rows = rx.items.map(item => {
      total += Number(item.unit_price || 0) * Number(item.quantity || 0);
      hiddenLines.insertAdjacentHTML('beforeend', `<input type="hidden" name="medicine_id" value="${item.medicine_id}"><input type="hidden" name="quantity" value="${item.quantity}">`);
      const stockClass = Number(item.stock) >= Number(item.quantity) ? 'available' : 'short';
      const stockText = stockClass === 'available' ? `${item.stock} in stock` : `Only ${item.stock} available`;
      return `<tr><td><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.strength)}</small></td><td>${escapeHtml(item.dosage)}</td><td>${escapeHtml(item.duration)}</td><td>${item.quantity}</td><td><span class="${stockClass}">${stockText}</span></td><td>${money(Number(item.unit_price) * Number(item.quantity))}</td></tr>`;
    }).join('');
    rxPanel.innerHTML = `<header><div><small>DOCTOR PRESCRIPTION · RX-${String(rx.id).padStart(6,'0')}</small><h3>${escapeHtml(rx.patient)}'s prescribed medicines</h3><p>Prescribed by ${escapeHtml(rx.doctor)} · ${escapeHtml(rx.created)}</p></div><strong>Pending dispense</strong></header><div class="pos-rx-table-wrap"><table><thead><tr><th>Medicine</th><th>Dosage</th><th>Duration</th><th>Qty</th><th>Stock</th><th>Amount</th></tr></thead><tbody>${rows}</tbody></table></div>${rx.notes ? `<p class="pos-rx-notes"><b>Doctor notes:</b> ${escapeHtml(rx.notes)}</p>` : ''}<footer><span>Prescription total</span><b>${money(total)}</b></footer>`;
  };
  patient.addEventListener('change', showPatientPrescription);
  showPatientPrescription();
})();
