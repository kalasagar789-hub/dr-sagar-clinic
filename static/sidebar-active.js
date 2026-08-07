(() => {
  const brand = document.querySelector('aside .brand');
  if (brand) brand.innerHTML = '<b>✚</b> Dr. Sagar\'s Clinic';
  const sidebar = document.querySelector('aside nav');
  if (sidebar && document.querySelector('aside.admin-persistent-nav') && !sidebar.querySelector('[href="/admin/finance"]')) {
    const link = document.createElement('a');
    link.href = '/admin/finance'; link.textContent = 'Finance & Payroll';
    const signOut = [...sidebar.querySelectorAll('a')].find(item => /sign out/i.test(item.textContent));
    sidebar.insertBefore(link, signOut || null);
  }
  const role = document.querySelector('aside .role small')?.textContent.trim().toLowerCase();
  if (sidebar && role === 'patient' && !sidebar.querySelector('[href="/patient-portal"]')) {
    const link = document.createElement('a');
    link.href = '/patient-portal';
    link.textContent = 'My Health Portal';
    const signOut = [...sidebar.querySelectorAll('a')].find(item => /sign out/i.test(item.textContent));
    sidebar.insertBefore(link, signOut || null);
  }
  const icons = { Dashboard: '⌂', 'Patient Overview': '◉', 'Reception Desk': '▣', Consultation: '⚕', Dietician: '♧', Laboratory: '⚗', Pharmacy: '✚', Administration: '⚙', 'My Health Portal': '♡', 'Sign out': '↪' };
  document.querySelectorAll('aside nav a').forEach(link => {
    const label = link.dataset.module || link.textContent.trim();
    link.dataset.module = label;
    if (icons[label] && !link.querySelector('.side-icon')) link.innerHTML = `<i class="side-icon" aria-hidden="true">${icons[label]}</i><span>${label}</span>`;
  });
  if (sidebar && !document.getElementById('sidebar-icon-style')) {
    const style = document.createElement('style'); style.id = 'sidebar-icon-style';
    style.textContent = 'aside nav a{display:flex!important;align-items:center;gap:10px}aside nav a .side-icon{display:grid;place-items:center;width:23px;height:23px;border-radius:7px;background:#ffffff12;color:#67e8d6;font-style:normal;font-size:15px;line-height:1}aside nav a:hover .side-icon,aside nav a.active-module .side-icon{background:#ffffff25;color:#fff}';
    document.head.append(style);
  }
  const path = location.pathname;
  const sectionForPath = () => {
    if (path.startsWith('/dashboard')) return 'Dashboard';
    if (path.startsWith('/patient-portal')) return 'My Health Portal';
    if (path.startsWith('/patients/')) return 'Patient Overview';
    if (path.startsWith('/appointments')) return 'Reception Desk';
    if (path.startsWith('/encounter') || path.startsWith('/consultation')) return 'Consultation';
    if (path.startsWith('/dietician')) return 'Dietician';
    if (path.startsWith('/labs') || path.startsWith('/lab-inventory')) return 'Laboratory';
    if (path.startsWith('/pharmacy')) return 'Pharmacy';
    if (path.startsWith('/admin/finance')) return 'Finance & Payroll';
    if (path.startsWith('/admin')) return 'Administration';
    return '';
  };
  const active = sectionForPath();
  document.querySelectorAll('aside nav a').forEach(link => {
    link.style.setProperty('background', 'transparent', 'important');
    link.style.setProperty('color', '#eaf5ff', 'important');
    if (link.dataset.module === active) {
      link.classList.add('active-module');
      link.style.setProperty('background', '#1a527e', 'important');
      link.style.setProperty('color', '#ffffff', 'important');
    }
  });

  // The same real search is available in laboratory, staff, admin and dashboard
  // workspaces. Existing decorative search areas are replaced without changing
  // the module layouts.
  if (!['/login', '/forgot-password', '/reset-password'].includes(location.pathname) && !document.querySelector('.global-clinic-search')) {
    const search = document.createElement('form');
    search.className = 'global-clinic-search';
    search.method = 'get';
    search.action = '/search';
    search.innerHTML = '<input name="q" type="search" minlength="2" maxlength="80" required placeholder="Search patient, UHID, mobile, lab test or order ID" aria-label="Search clinic records"><button aria-label="Search clinic records" title="Search">⌕</button>';
    const existing = document.querySelector('.lab-top-search, .staff-search');
    if (existing) existing.replaceWith(search);
    else {
      const pageHeader = document.querySelector('main > header');
      if (pageHeader) pageHeader.append(search);
    }
    const style = document.createElement('style');
    style.id = 'global-clinic-search-style';
    style.textContent = '.global-clinic-search{display:flex;align-items:center;width:min(430px,42vw);min-width:245px;height:44px;border:1px solid #bfd9e8;border-radius:8px;background:#fff;overflow:hidden;box-shadow:0 2px 8px #123b5d12}.global-clinic-search input{flex:1;min-width:0;border:0;outline:0;padding:0 13px;color:#24475f;font:13px Arial;background:transparent}.global-clinic-search button{width:45px;height:100%;border:0;border-left:1px solid #e3edf1;background:#fff;color:#075aab;font-size:20px;cursor:pointer}.global-clinic-search button:hover{background:#edf8ff}main>header .global-clinic-search{margin-left:auto}@media(max-width:700px){.global-clinic-search{width:100%;min-width:0}main>header{flex-wrap:wrap;gap:12px}main>header .global-clinic-search{margin-left:0}.laboratory-topbar .global-clinic-search,.staff-topbar .global-clinic-search{order:4;flex-basis:100%}}';
    document.head.append(style);
  }

  // Free local workflow insights make the existing Dashboard AI card useful
  // without sending clinic or patient information to an external AI service.
  const insightButton = document.querySelector('.ai-card button');
  const insightCard = document.querySelector('.ai-card');
  if (insightButton && insightCard && !insightCard.dataset.insightsReady) {
    insightCard.dataset.insightsReady = 'true';
    const output = document.createElement('div');
    output.className = 'local-ai-insights';
    output.hidden = true;
    insightCard.append(output);
    insightButton.type = 'button';
    insightButton.textContent = 'Generate free insights';
    insightButton.addEventListener('click', async () => {
      insightButton.disabled = true;
      insightButton.textContent = 'Reviewing workflow…';
      try {
        const response = await fetch('/api/clinic-ai-insights');
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error('Unavailable');
        output.replaceChildren();
        data.insights.forEach(item => {
          const insight = document.createElement('article');
          insight.className = `local-ai-insight ${item.tone || 'info'}`;
          const title = document.createElement('b'); title.textContent = item.title;
          const detail = document.createElement('small'); detail.textContent = item.detail;
          insight.append(title, detail); output.append(insight);
        });
        const disclaimer = document.createElement('small');
        disclaimer.className = 'local-ai-disclaimer';
        disclaimer.textContent = data.disclaimer;
        output.append(disclaimer); output.hidden = false;
        insightButton.textContent = 'Refresh free insights';
      } catch (_) {
        output.textContent = 'Insights are unavailable. Please review the dashboard manually.';
        output.hidden = false; insightButton.textContent = 'Try again';
      } finally { insightButton.disabled = false; }
    });
    const style = document.createElement('style');
    style.textContent = '.local-ai-insights{display:grid;gap:7px;margin-top:12px}.local-ai-insight{padding:9px;border:1px solid #ffffff36;border-radius:8px;background:#ffffff12;font-size:11px;line-height:1.42}.local-ai-insight b{display:block;color:#fff}.local-ai-insight small{display:block;margin-top:3px;color:#d8f7f1;font-size:10px}.local-ai-insight.warning,.local-ai-insight.attention{border-color:#ffd28a;background:#fff4d817}.local-ai-insight.success{border-color:#a7f0d9;background:#d8fff018}.local-ai-disclaimer{color:#d8f7f1;font-size:9px;line-height:1.35}.ai-card button{cursor:pointer}.ai-card button:disabled{opacity:.7;cursor:wait}';
    document.head.append(style);
  }

  // Staff registration belongs inside the main Administration screen so access,
  // approvals and reset-email management are handled together.
  const adminPage = document.querySelector('.admin-page');
  if (adminPage && !document.querySelector('.admin-staff-management')) {
    const card = document.createElement('section');
    card.className = 'admin-card admin-staff-management';
    card.innerHTML = '<div class="card-title"><div><h3>Register clinic employee</h3><p>Create a role-based account. Give the temporary password privately; it is never shown again.</p></div><b>Staff access</b></div><form class="admin-staff-create"><label>Full name<input name="name" required maxlength="100" placeholder="e.g. Anjali Rao"></label><label>Clinic email<input name="email" type="email" required maxlength="120" placeholder="employee@yourclinic.com"></label><label>Mobile number<input name="phone" inputmode="numeric" maxlength="10" placeholder="Optional 10-digit mobile"></label><label>Role<select name="role" required><option value="">Select role</option><option value="doctor">Doctor</option><option value="reception">Reception</option><option value="lab">Laboratory staff</option><option value="pharmacy">Pharmacy staff</option><option value="dietician">Dietician</option></select></label><label>Temporary password<input name="password" type="password" minlength="10" required placeholder="10+ characters with letter and number"></label><label class="admin-approve-now"><input name="approved" type="checkbox"> Approve account now</label><button>Create staff account</button></form><p class="admin-staff-message" aria-live="polite"></p>';
    const services = adminPage.querySelector('.services');
    services?.insertAdjacentElement('beforebegin', card) || adminPage.append(card);
    const form = card.querySelector('form'); const message = card.querySelector('.admin-staff-message');
    form.addEventListener('submit', async event => {
      event.preventDefault(); const button = form.querySelector('button'); button.disabled = true; button.textContent = 'Creating…';
      const values = new FormData(form);
      const payload = Object.fromEntries(values.entries()); payload.approved = values.get('approved') === 'on';
      try {
        const response = await fetch('/api/admin/staff', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
        const result = await response.json();
        message.textContent = result.message || 'Unable to create employee account.';
        message.dataset.tone = response.ok && result.ok ? 'success' : 'error';
        if (response.ok && result.ok) { form.reset(); setTimeout(() => location.reload(), 850); }
      } catch (_) { message.textContent = 'Unable to create employee account. Please try again.'; message.dataset.tone = 'error'; }
      finally { button.disabled = false; button.textContent = 'Create staff account'; }
    });
    const style = document.createElement('style');
    style.textContent = '.admin-staff-management{margin-bottom:18px}.admin-staff-create{display:grid;grid-template-columns:1.2fr 1.2fr .9fr 1fr 1.25fr auto;gap:10px;align-items:end;padding:16px}.admin-staff-create label{display:grid;gap:5px;color:#526e82;font-size:11px;font-weight:800}.admin-staff-create input,.admin-staff-create select{min-width:0;box-sizing:border-box;width:100%;padding:9px;border:1px solid #c9dce6;border-radius:7px;background:#fff;color:#183c59;font-size:12px}.admin-staff-create .admin-approve-now{display:flex;align-items:center;gap:7px;padding-bottom:9px;color:#17786e}.admin-staff-create .admin-approve-now input{width:16px;height:16px}.admin-staff-create button{border:0;border-radius:7px;padding:10px 12px;background:linear-gradient(100deg,#168d80,#1565c0);color:#fff;font-size:12px;font-weight:800;cursor:pointer}.admin-staff-create button:disabled{opacity:.7;cursor:wait}.admin-staff-message{margin:0;padding:0 16px 15px;color:#14796f;font-size:12px;font-weight:700}.admin-staff-message[data-tone=error]{color:#b4232c}@media(max-width:1050px){.admin-staff-create{grid-template-columns:repeat(2,minmax(0,1fr))}.admin-staff-create button{justify-self:start}}@media(max-width:560px){.admin-staff-create{grid-template-columns:1fr}}';
    document.head.append(style);
  }

  // Administrators maintain the address used for password-reset OTP delivery.
  const staffList = document.querySelector('.admin-page .staff-list');
  if (staffList && !staffList.dataset.resetEmailReady) {
    staffList.dataset.resetEmailReady = 'true';
    fetch('/api/admin/staff-reset-emails').then(response => response.ok ? response.json() : Promise.reject()).then(data => {
      staffList.innerHTML = '';
      data.staff.forEach(staff => {
        const row = document.createElement('div');
        const details = document.createElement('span');
        const name = document.createElement('b'); name.textContent = staff.name;
        const email = document.createElement('small'); email.textContent = `${staff.role} · ${staff.email || 'No reset email configured'}`;
        details.append(name, email);
        const actions = document.createElement('span'); actions.className = 'staff-email-actions';
        const state = document.createElement('em'); state.className = staff.approved ? 'approved' : 'pending'; state.textContent = staff.approved ? 'Approved' : 'Pending';
        const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Set reset email';
        actions.append(state, button); row.append(details, actions);
        button.addEventListener('click', async () => {
          const email = prompt(`Reset email for ${staff.name}:`, staff.email || '');
          if (email === null) return;
          const response = await fetch(`/api/admin/staff/${staff.id}/reset-email`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email }) });
          const result = await response.json();
          if (!response.ok || !result.ok) return alert(result.message || 'Unable to update the reset email.');
          staff.email = result.email;
          email.textContent = `${staff.role} · ${staff.email}`;
          alert('Reset email updated.');
        });
        staffList.append(row);
      });
    }).catch(() => { /* Existing staff directory remains available if the API is unavailable. */ });
    const style = document.createElement('style');
    style.textContent = '.staff-email-actions{display:flex;align-items:center;gap:8px}.staff-email-actions button{border:1px solid #a9dcd5;border-radius:7px;background:#effaf8;color:#137c71;padding:7px 9px;font-size:11px;font-weight:800;cursor:pointer}.staff-email-actions button:hover{background:#dff5f0}@media(max-width:600px){.staff-list>div{align-items:flex-start;gap:10px;flex-direction:column}}';
    document.head.append(style);
  }
})();
