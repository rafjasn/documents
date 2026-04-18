'use strict';

let counter = 0;

function v4() {
    counter++;
    const hex = counter.toString(16).padStart(12, '0');
    return `00000000-0000-4000-8000-${hex}`;
}

function reset() {
    counter = 0;
}

module.exports = { v4, reset };
