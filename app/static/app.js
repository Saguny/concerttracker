

(function() {
  const _VIEW_KEY = 'moshd-show-view';
  function _apply(regionId, mode) {
    const el = document.getElementById(regionId);
    if (!el) return;
    const list = el.querySelector('.show-list');
    if (list) list.classList.toggle('show-grid', mode === 'grid');
  }
  function _initToggle(toggle) {
    const regionId = toggle.dataset.region;
    const mode = localStorage.getItem(_VIEW_KEY) || 'list';
    _apply(regionId, mode);
    toggle.querySelectorAll('.view-toggle-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === mode);
      btn.addEventListener('click', function() {
        const m = this.dataset.view;
        localStorage.setItem(_VIEW_KEY, m);
        _apply(regionId, m);
        toggle.querySelectorAll('.view-toggle-btn').forEach(b => b.classList.toggle('active', b.dataset.view === m));
      });
    });
  }
  window._initViewToggles = function() {
    document.querySelectorAll('.view-toggle[data-region]').forEach(_initToggle);
  };
  document.addEventListener('DOMContentLoaded', window._initViewToggles);
})();

document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => el.style.opacity = '0', 4000);
  el.style.transition = 'opacity .4s';
});

function _clipCopy(text, btn) {
  const label = btn ? btn.textContent : '';
  const done = () => { if (btn) { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = label; }, 2000); } };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:-999px;left:-999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch(e) {}
    document.body.removeChild(ta);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  } else {
    fallback();
  }
}

function _xpost(url, formData) {
  return fetch(url, {
    method: 'POST',
    body: formData,
    headers: { 'X-Requested-With': 'fetch' }
  }).then(r => r.json());
}

function _esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _getCSRF() {
  return document.querySelector('[name="_csrf"]')?.value || '';
}

function _buildComment(c, deleteUrl) {
  const csrf = _getCSRF();
  const av = c.avatar_url
    ? `<img src="${_esc(c.avatar_url)}" alt="${_esc(c.username)}">`
    : `<div class="comment-avatar-ph">${_esc((c.username || '?')[0].toUpperCase())}</div>`;
  const del = deleteUrl
    ? `<form method="post" action="${_esc(deleteUrl)}" class="comment-delete-form">
         <input type="hidden" name="_csrf" value="${_esc(csrf)}">
         <button type="submit" class="btn-icon-danger" title="Delete">&times;</button>
       </form>`
    : '';
  return `<div class="comment">
    <div class="comment-avatar">${av}</div>
    <div class="comment-body">
      <div class="comment-meta">
        <a href="/concert-tracker/u/${_esc(c.username)}" class="comment-author">${_esc(c.username)}</a>
        <span class="muted small">just now</span>
      </div>
      <div class="comment-text">${c.body}</div>
    </div>${del}
  </div>`;
}

function _syncCommentCount(section) {
  const list = section && section.querySelector('.comment-list');
  const span = section && section.querySelector('h3 .muted');
  if (span) span.textContent = list ? list.querySelectorAll('.comment').length : 0;
}

function _initComments(currentUser, currentAvatar) {
  const section = document.querySelector('.comments-section');
  if (!section || section.dataset.ajaxBound) return;
  section.dataset.ajaxBound = '1';

  const postForm = section.querySelector('.comment-form');
  if (!postForm) return;
  const postUrl = postForm.action;
  const deleteBase = postUrl;

  postForm.addEventListener('submit', async e => {
    e.preventDefault();
    const textarea = postForm.querySelector('textarea');
    if (!textarea.value.trim()) return;
    const btn = postForm.querySelector('button[type="submit"]');
    btn.disabled = true;
    const data = await _xpost(postUrl, new FormData(postForm));
    btn.disabled = false;
    if (!data.id) return;
    let list = section.querySelector('.comment-list');
    if (!list) {
      list = document.createElement('div');
      list.className = 'comment-list';
      section.insertBefore(list, postForm);
    }
    list.insertAdjacentHTML('beforeend', _buildComment(
      { id: data.id, body: data.body, username: currentUser, avatar_url: currentAvatar || null },
      `${deleteBase}/${data.id}/delete`
    ));
    textarea.value = '';
    _syncCommentCount(section);
  });

  section.addEventListener('submit', async e => {
    if (!e.target.matches('.comment-delete-form')) return;
    e.preventDefault();
    const form = e.target;
    const comment = form.closest('.comment');
    const data = await _xpost(form.action, new FormData(form));
    if (data.ok) { comment.remove(); _syncCommentCount(section); }
  });
}

function _initLike() {
  const form = document.querySelector('.like-form');
  if (!form || form.dataset.ajaxBound) return;
  form.dataset.ajaxBound = '1';
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const data = await _xpost(form.action, new FormData(form));
    const btn = form.querySelector('.btn-like');
    btn.classList.toggle('liked', data.liked);
    btn.innerHTML = (data.liked ? '&#x2665;' : '&#x2661;') + ' <span>' + data.count + '</span>';
  });
}

function _debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function _initFilterAjax(formId, regionId, countId) {
  const form = document.getElementById(formId);
  if (!form) return;

  const doFilter = _debounce(async () => {
    const qs = new URLSearchParams(new FormData(form)).toString();
    const url = form.action + (qs ? '?' + qs : '');
    const res = await fetch(url);
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const newRegion = doc.getElementById(regionId);
    if (newRegion) document.getElementById(regionId).replaceWith(newRegion);
    if (countId) {
      const newCount = doc.getElementById(countId);
      if (newCount) document.getElementById(countId).textContent = newCount.textContent;
    }
    history.replaceState(null, '', url);
  }, 250);

  form.querySelectorAll('select').forEach(s => s.addEventListener('change', doFilter));
  form.querySelector('input[type="text"]')?.addEventListener('input', doFilter);
  form.addEventListener('submit', e => e.preventDefault());
}

function _initShowDeletes(regionId, countId) {
  document.addEventListener('submit', async e => {
    const form = e.target;
    if (!form.matches('form[action*="/delete"]')) return;
    if (form.closest('.comment') || form.matches('.comment-delete-form')) return;
    const card = form.closest('.show-card');
    if (!card) return;
    e.preventDefault();
    const label = card.querySelector('.show-artist')?.textContent?.trim() || 'this';
    if (!confirm(`Delete ${label}?`)) return;
    const data = await _xpost(form.action, new FormData(form));
    if (data.ok) {
      card.remove();
      if (countId) {
        const countEl = document.getElementById(countId);
        if (countEl) countEl.textContent = document.querySelectorAll('.show-card').length;
      }
    }
  }, true);
}

// ── @ Mention autocomplete in textareas ──────────────────────────────────────
function _initMentionAutocomplete(textarea) {
  if (!textarea || textarea.dataset.mentionBound) return;
  textarea.dataset.mentionBound = '1';

  const list = document.createElement('ul');
  list.className = 'mention-ac-list';
  list.style.display = 'none';
  document.body.appendChild(list);

  let _t = null;
  let _keysInited = false;

  function _pos() {
    const r = textarea.getBoundingClientRect();
    list.style.left = r.left + 'px';
    list.style.width = r.width + 'px';
    list.style.top = (r.bottom + window.scrollY + 4) + 'px';
  }

  function _query() {
    const before = textarea.value.slice(0, textarea.selectionStart);
    const m = before.match(/@([A-Za-z0-9_]{0,30})$/);
    return m ? m[1] : null;
  }

  function _hide() { list.style.display = 'none'; list.innerHTML = ''; _keysInited = false; }

  function _insert(username) {
    const before = textarea.value.slice(0, textarea.selectionStart);
    const after = textarea.value.slice(textarea.selectionStart);
    const replaced = before.replace(/@([A-Za-z0-9_]{0,30})$/, '@' + username + ' ');
    textarea.value = replaced + after;
    textarea.selectionStart = textarea.selectionEnd = replaced.length;
    textarea.focus();
    _hide();
  }

  function _render(users) {
    if (!users.length) { _hide(); return; }
    list.innerHTML = users.map(u => {
      const av = u.avatar_url
        ? `<img src="${_esc(u.avatar_url)}" alt="" class="mention-ac-avatar">`
        : `<div class="mention-ac-avatar-ph">${_esc((u.username || '?')[0].toUpperCase())}</div>`;
      return `<li data-username="${_esc(u.username)}">${av}<span>@${_esc(u.username)}</span></li>`;
    }).join('');
    _pos();
    list.style.display = 'block';
    if (!_keysInited && typeof _initAutocompleteKeys === 'function') {
      _initAutocompleteKeys(textarea, list, li => _insert(li.dataset.username));
      _keysInited = true;
    }
    list.querySelectorAll('li').forEach(li => {
      li.addEventListener('mousedown', e => { e.preventDefault(); _insert(li.dataset.username); });
    });
  }

  textarea.addEventListener('input', () => {
    clearTimeout(_t);
    const q = _query();
    if (q === null || q.length < 1) { _hide(); return; }
    _t = setTimeout(async () => {
      try {
        const r = await fetch(`/concert-tracker/api/user-search?q=${encodeURIComponent(q)}`, { headers: { 'X-Requested-With': 'fetch' } });
        _render(await r.json());
      } catch(e) { _hide(); }
    }, 150);
  });

  document.addEventListener('click', e => {
    if (e.target !== textarea && !list.contains(e.target)) _hide();
  }, true);
}

document.querySelectorAll('.comment-form textarea').forEach(_initMentionAutocomplete);

// ── Follow / unfollow (delegated -works on profile page and follow list) ────

document.addEventListener('submit', async e => {
  const form = e.target;
  if (!form.action) return;
  const isFollow = form.action.endsWith('/concert-tracker/u/follow');
  const isUnfollow = form.action.endsWith('/concert-tracker/u/unfollow');
  if (!isFollow && !isUnfollow) return;
  e.preventDefault();

  const csrf = _getCSRF();
  const username = (form.querySelector('[name="follow_user"]') || form.querySelector('[name="username"]'))?.value;
  if (!username) return;

  const data = await _xpost(form.action, new FormData(form));

  const profileActions = form.closest('.profile-actions');
  if (profileActions) {
    profileActions.innerHTML = data.following
      ? `<form method="post" action="/concert-tracker/u/unfollow">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="username" value="${_esc(username)}">
           <button class="btn btn-ghost">Unfollow</button>
         </form>`
      : `<form method="post" action="/concert-tracker/u/follow">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="follow_user" value="${_esc(username)}">
           <button class="btn btn-accent">Follow</button>
         </form>`;
    return;
  }

  const row = form.closest('.follow-list-row');
  if (row) {
    form.outerHTML = data.following
      ? `<form method="post" action="/concert-tracker/u/unfollow" style="margin-left:auto">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="username" value="${_esc(username)}">
           <button class="btn btn-sm btn-ghost">Unfollow</button>
         </form>`
      : `<form method="post" action="/concert-tracker/u/follow" style="margin-left:auto">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="follow_user" value="${_esc(username)}">
           <button class="btn btn-sm btn-accent">Follow</button>
         </form>`;
    return;
  }

  const card = form.closest('.discover-card');
  if (card) {
    form.outerHTML = data.following
      ? `<form method="post" action="/concert-tracker/u/unfollow">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="username" value="${_esc(username)}">
           <button class="btn btn-sm btn-ghost">Unfollow</button>
         </form>`
      : `<form method="post" action="/concert-tracker/u/follow">
           <input type="hidden" name="_csrf" value="${_esc(csrf)}">
           <input type="hidden" name="follow_user" value="${_esc(username)}">
           <button class="btn btn-sm btn-accent">Follow</button>
         </form>`;
  }
});

window._initAutocompleteKeys = function(input, list, onSelect) {
  input.addEventListener('keydown', function(e) {
    const items = Array.from(list.querySelectorAll('li'));
    if (!items.length || list.style.display === 'none') return;
    const active = list.querySelector('li.ac-active');
    const idx = active ? items.indexOf(active) : -1;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = items[(idx + 1) % items.length];
      if (active) active.classList.remove('ac-active');
      next.classList.add('ac-active');
      next.scrollIntoView({block:'nearest'});
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = items[(idx - 1 + items.length) % items.length];
      if (active) active.classList.remove('ac-active');
      prev.classList.add('ac-active');
      prev.scrollIntoView({block:'nearest'});
    } else if (e.key === 'Enter' && active) {
      e.preventDefault();
      onSelect(active);
    } else if (e.key === 'Escape') {
      list.style.display = 'none';
      items.forEach(i => i.classList.remove('ac-active'));
    }
  });
};
