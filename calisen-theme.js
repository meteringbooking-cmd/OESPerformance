/* Calisen theme bootstrap: runs in <head> before first paint so there is no flash.
   Light workspace is the default; a user's explicit "dark" choice (localStorage calisen-theme) is respected. */
(function () {
  var root = document.documentElement;
  try { if (localStorage.getItem('calisen-theme') !== 'dark') root.classList.add('light-mode'); }
  catch (e) { root.classList.add('light-mode'); }
})();
