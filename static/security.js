/* Adds the session CSRF token without altering any existing form or module UI. */
(() => {
  if (['/forgot-password', '/reset-password'].includes(location.pathname) && !document.querySelector('link[href*="reset-theme.css"]')) {
    const theme = document.createElement('link');
    theme.rel = 'stylesheet'; theme.href = '/static/reset-theme.css?v=20260807';
    document.head.append(theme);
  }
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (!token) return;

  document.querySelectorAll('form').forEach((form) => {
    const method = (form.getAttribute('method') || 'get').toLowerCase();
    if (!['post', 'put', 'patch', 'delete'].includes(method) || form.querySelector('input[name="csrf_token"]')) return;
    const field = document.createElement('input');
    field.type = 'hidden';
    field.name = 'csrf_token';
    field.value = token;
    form.append(field);
  });

  const originalFetch = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    const method = (options.method || (resource instanceof Request ? resource.method : 'GET')).toUpperCase();
    if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) return originalFetch(resource, options);
    const headers = new Headers(options.headers || (resource instanceof Request ? resource.headers : undefined));
    headers.set('X-CSRF-Token', token);
    return originalFetch(resource, { ...options, headers });
  };

  const loginForm = document.querySelector('#clinic-login-form');
  if (loginForm && !document.querySelector('.forgot-password-link')) {
    const link = document.createElement('a');
    link.className = 'forgot-password-link';
    link.href = '/forgot-password';
    link.textContent = 'Forgot staff password?';
    link.style.cssText = 'display:block;margin:13px 0 0;text-align:center;color:#147ca1;font-size:12px;font-weight:700;text-decoration:none';
    loginForm.after(link);
  }
})();
