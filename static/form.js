// Скрипты формы заявки: быстрый выбор дня и кнопка геолокации.
(function () {
  var box = document.getElementById('date-pick');
  if (!box) return;
  var t = box.getAttribute('data-today').split('-');
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  var day = box.querySelector('[name=agreed_day]');
  var month = box.querySelector('[name=agreed_month]');
  var time = box.querySelector('[name=agreed_time]');
  var curMonth = t[0] + '-' + t[1];
  document.querySelectorAll('[data-offset]').forEach(function (b) {
    b.addEventListener('click', function () {
      var d = new Date(Date.UTC(+t[0], +t[1] - 1, +t[2] + (+b.getAttribute('data-offset'))));
      day.value = String(d.getUTCDate());
      month.value = d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1);
    });
  });
  document.getElementById('date-none').addEventListener('click', function () {
    day.value = '';
    time.value = '';
    month.value = curMonth;
  });
})();
(function () {
  var btn = document.getElementById('geo-btn');
  var status = document.getElementById('geo-status');
  var input = document.getElementById('location');
  if (!btn) return;
  btn.addEventListener('click', function () {
    if (!window.isSecureContext || !navigator.geolocation) {
      status.textContent = 'Телефон даёт местоположение только на защищённом адресе (https). Пока вставьте ссылку на карту вручную — после запуска в интернете кнопка заработает.';
      return;
    }
    status.textContent = 'Определяем местоположение…';
    navigator.geolocation.getCurrentPosition(function (pos) {
      input.value = pos.coords.latitude.toFixed(6) + ', ' + pos.coords.longitude.toFixed(6);
      status.textContent = 'Готово. Точность около ' + Math.round(pos.coords.accuracy) + ' м.';
    }, function (err) {
      if (err && err.code === 1) {
        status.textContent = 'Доступ к местоположению запрещён. Разрешите его в настройках браузера или вставьте ссылку вручную.';
      } else {
        status.textContent = 'Не удалось определить местоположение. Попробуйте ещё раз или вставьте ссылку вручную.';
      }
    }, { enableHighAccuracy: true, timeout: 15000 });
  });
})();
