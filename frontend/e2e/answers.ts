/** Checks of an answer's text, tolerant of how the model writes numbers and dates. */

const SPACES = /[  ]/g;

function grouped(integer: string, separator: string): string {
    return integer.replace(/\B(?=(\d{3})+(?!\d))/g, separator);
}

/** The amount in any common English or Polish format: 3331.40, 3,331.40, 3 331,40. */
export function hasAmount(text: string, value: number): boolean {
    const plain = text.replace(SPACES, ' ');
    const [integer, decimals] = value.toFixed(2).split('.');
    const forms = new Set([
        `${integer}.${decimals}`,
        `${integer},${decimals}`,
        `${grouped(integer, ',')}.${decimals}`,
        `${grouped(integer, ' ')},${decimals}`,
        `${grouped(integer, ' ')}.${decimals}`,
        `${grouped(integer, '.')},${decimals}`
    ]);
    return [...forms].some((form) =>
        new RegExp(`(^|[^\\d.,])${form.replace(/\./g, '\\.')}(?!\\d)`).test(plain)
    );
}

const MONTHS_EN = [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December'
];
const MONTHS_PL = [
    'stycznia',
    'lutego',
    'marca',
    'kwietnia',
    'maja',
    'czerwca',
    'lipca',
    'sierpnia',
    'września',
    'października',
    'listopada',
    'grudnia'
];

/** The date as ISO, 20.09.2026, 20/09/2026, 20 September 2026, September 20, 2026 or
 * 20 września 2026. */
export function hasDate(text: string, iso: string): boolean {
    const plain = text.replace(SPACES, ' ');
    const [year, month, day] = iso.split('-');
    const d = String(Number(day));
    const m = Number(month) - 1;
    return [
        iso,
        `${day}.${month}.${year}`,
        `${day}/${month}/${year}`,
        `${d} ${MONTHS_EN[m]} ${year}`,
        `${MONTHS_EN[m]} ${d}, ${year}`,
        `${d} ${MONTHS_PL[m]} ${year}`
    ].some((form) => plain.includes(form));
}

/** The answer says the documents don't give the fact. */
export function isRefusal(text: string): boolean {
    return /(do(es)? not|don't|doesn't) (state|contain|specify|include|mention|provide|list)|not (stated|specified|provided|mentioned|listed|included)|nie (zawiera|podaj|określa|wskazuj)/i.test(
        text
    );
}
