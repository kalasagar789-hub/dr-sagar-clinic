(() => {
  const pharmacyHero = document.querySelector('.pharm-hero > div:last-child');
  if (pharmacyHero && !pharmacyHero.querySelector('.collections-link')) {
    const link = document.createElement('a');
    link.className = 'collections-link';
    link.href = '/billing/collections';
    link.textContent = 'Payments & collections';
    pharmacyHero.append(link);
  }
  document.querySelectorAll('.pharm-hero a[href="#add-batch"], .pharm-hero a[href="#new-medicine"]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault(); location.href = `/pharmacy?tab=stock${link.getAttribute('href')}`;
  }));
  const form = document.querySelector('form.pos-grid');
  if (!form) return;
  const medicine = form.querySelector('select[name="medicine_id"]');
  const quantity = form.querySelector('input[name="quantity"]');
  if (!medicine || !quantity) return;
  const prescription = window.PHARMACY_RX;
  const patient = form.querySelector('select[name="patient_id"]');
  if (prescription && patient) {
    patient.value = String(prescription.patient_id);
    const firstItem = prescription.items?.[0];
    if (firstItem) {
      medicine.value = String(firstItem.medicine_id);
      quantity.value = String(firstItem.quantity);
    }
    let prescriptionId = form.querySelector('input[name="prescription_id"]');
    if (!prescriptionId) {
      prescriptionId = document.createElement('input');
      prescriptionId.type = 'hidden';
      prescriptionId.name = 'prescription_id';
      form.append(prescriptionId);
    }
    prescriptionId.value = String(prescription.id);
  }
  const preview = document.createElement('section'); preview.className = 'pos-medicine-preview'; preview.innerHTML = '<b>Medicine invoice details</b><span>Select a medicine to load batch, expiry, MRP, GST and stock.</span>';
  form.insertBefore(preview, form.firstChild.nextSibling);
  const loadDetails = () => {
    if (!medicine.value) return;
    fetch(`/api/pharmacy/medicine/${medicine.value}/billing-details`).then(r => r.json()).then(data => {
      const qty = Number(quantity.value || 1); const value = qty * Number(data.price || 0);
      preview.innerHTML = `<b>${data.name} ${data.strength}</b><span>Batch: ${data.batch} · Expiry: ${data.expiry} · Available: ${data.available}</span><span>MRP: ₹${Number(data.mrp).toFixed(2)} · GST: ${data.gst}% · Line amount: ₹${value.toFixed(2)}</span>`;
    }).catch(() => {});
  };
  medicine.addEventListener('change', loadDetails); quantity.addEventListener('input', loadDetails); loadDetails();
  const lines = document.createElement('div'); lines.className = 'pos-extra-lines';
  const add = document.createElement('button'); add.type = 'button'; add.className = 'pos-add-line'; add.textContent = '+ Add another medicine';
  add.onclick = () => {
    const row = document.createElement('div'); row.className = 'pos-extra-line';
    const select = medicine.cloneNode(true); select.value = '';
    const input = quantity.cloneNode(true); input.value = '1';
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove'; remove.onclick = () => row.remove();
    const rowPreview = document.createElement('small'); rowPreview.textContent = 'Batch and invoice details are selected automatically at billing.';
    row.append(select, input, remove, rowPreview); lines.append(row);
  };
  if (prescription?.items?.length > 1) {
    prescription.items.slice(1).forEach(item => {
      const row = document.createElement('div'); row.className = 'pos-extra-line';
      const select = medicine.cloneNode(true); select.value = String(item.medicine_id); select.name = 'medicine_id';
      const input = quantity.cloneNode(true); input.value = String(item.quantity); input.name = 'quantity';
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove'; remove.onclick = () => row.remove();
      const rowPreview = document.createElement('small'); rowPreview.textContent = 'Added automatically from the doctor prescription.';
      row.append(select, input, remove, rowPreview); lines.append(row);
    });
  }
  form.insertBefore(lines, form.querySelector('button[type="submit"]'));
  form.insertBefore(add, lines);
})();
