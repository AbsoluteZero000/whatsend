import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.services.message_variables import ARABIC_ORDINAL_WORDS, arabic_ordinal, render_message


@pytest.mark.parametrize('number,expected', [
    (1, 'الأول'), (2, 'الثاني'), (3, 'الثالث'), (10, 'العاشر'),
    (11, 'الحادي عشر'), (12, 'الثاني عشر'), (19, 'التاسع عشر'),
    (20, 'العشرون'), (21, 'الحادي والعشرون'), (22, 'الثاني والعشرون'),
    (99, 'التاسع والتسعون'), (100, 'المائة'), (101, 'الأول بعد المائة'),
    (200, 'المائتان'), (221, 'الحادي والعشرون بعد المائتين'),
    (300, 'الثلاثمائة'), (999, 'التاسع والتسعون بعد التسعمائة'), (1000, 'الألف'),
])
def test_arabic_send_ordinals(number, expected):
    assert arabic_ordinal(number) == expected
    assert render_message('الدرس {send_ordinal_ar} ({send_number}) {literal}', number, 1000, 'UTC') == f'الدرس {expected} ({number}) {{literal}}'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node verifies preview and delivery agree')
def test_arabic_preview_matches_delivery_for_every_supported_send():
    script = Path(__file__).resolve().parents[1] / 'app/static/js/message-series.js'
    result = subprocess.run(['node', '-e', '''
      const fs = require('fs'), vm = require('vm');
      const window = {};
      vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window});
      const words = JSON.parse(fs.readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify(Array.from({length:1000}, (_,i)=>window.renderArabicOrdinal(i+1,words))));
    ''', str(script)], input=json.dumps(ARABIC_ORDINAL_WORDS), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [arabic_ordinal(number) for number in range(1, 1001)]
