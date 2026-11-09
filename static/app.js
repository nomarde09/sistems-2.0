// Общий скрипт: подтверждение перед важными действиями (форма с data-confirm).
document.addEventListener('submit', function (event) {
  var form = event.target;
  var text = form && form.getAttribute && form.getAttribute('data-confirm');
  if (text && !window.confirm(text)) {
    event.preventDefault();
  }
});
