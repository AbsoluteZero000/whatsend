import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

VARIABLES = {'send_number', 'send_ordinal_ar', 'counter', 'total_sends', 'today', 'date', 'time'}
PATTERN = re.compile(r'\{([a-z_]+)\}')
ARABIC_ORDINAL_WORDS = {
    'units': ['', 'الأول', 'الثاني', 'الثالث', 'الرابع', 'الخامس', 'السادس', 'السابع', 'الثامن', 'التاسع', 'العاشر',
              'الحادي عشر', 'الثاني عشر', 'الثالث عشر', 'الرابع عشر', 'الخامس عشر', 'السادس عشر', 'السابع عشر', 'الثامن عشر', 'التاسع عشر'],
    'tens': ['', '', 'العشرون', 'الثلاثون', 'الأربعون', 'الخمسون', 'الستون', 'السبعون', 'الثمانون', 'التسعون'],
    'hundreds': ['', 'المائة', 'المائتان', 'الثلاثمائة', 'الأربعمائة', 'الخمسمائة', 'الستمائة', 'السبعمائة', 'الثمانمائة', 'التسعمائة'],
    'compound_first': 'الحادي', 'after': 'بعد', 'two_hundred_after': 'المائتين', 'thousand': 'الألف',
}


def arabic_ordinal(number):
    """Masculine Arabic ordinals for the 1–1000 sends supported by a plan."""
    words = ARABIC_ORDINAL_WORDS
    if not 1 <= number <= 1000:
        return str(number)
    if number < 20:
        return words['units'][number]
    if number < 100:
        tens, unit = divmod(number, 10)
        prefix = words['compound_first'] if unit == 1 else words['units'][unit]
        return prefix + ' و' + words['tens'][tens] if unit else words['tens'][tens]
    if number == 1000:
        return words['thousand']
    hundreds, remainder = divmod(number, 100)
    base = words['two_hundred_after'] if hundreds == 2 and remainder else words['hundreds'][hundreds]
    return arabic_ordinal(remainder) + ' ' + words['after'] + ' ' + base if remainder else base


def render_message(message, number, total, timezone_name, now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name or 'UTC'))
    values = {'send_number': str(number), 'counter': str(number), 'total_sends': str(total) if total is not None else '∞',
              'send_ordinal_ar': arabic_ordinal(number),
              'today': local.strftime('%Y-%m-%d'), 'date': local.strftime('%Y-%m-%d'), 'time': local.strftime('%H:%M')}
    # Only replace the supported variables; ordinary braces remain literal text.
    return PATTERN.sub(lambda match: values.get(match[1], match[0]), message)
