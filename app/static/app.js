// Shared utilities used across pages

// Auto-dismiss flash messages after 4s
document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => el.style.opacity = '0', 4000);
  el.style.transition = 'opacity .4s';
});
