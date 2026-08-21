export const matchFixture = {
    match_id: 101,
    category: "cadets_woman_under_40kg",
    title: 'Quarterfinals',
    round: 3,
    clk: '1:45',
    win: 'blue',
    doctor: false,
    blue: {
        name: 'Andrej Hornak',
        flag2: 'sk',
        flag3: 'svk',
        points: [
            { points: 5, hits: 3, trunk: 1, rotation_trunk: 0, head: 0, rotation_head: 0, punch: 1, penalties: 0 },
            { points: 8, hits: 5, trunk: 2, rotation_trunk: 0, head: 0, rotation_head: 0, punch: 2, penalties: 0 },
            { points: 10, hits: 6, trunk: 2, rotation_trunk: 1, head: 0, rotation_head: 0, punch: 2, penalties: 1 },
        ],
    },
    red: {
        name: 'Matus Matejka',
        flag2: 'kr',
        flag3: 'kor',
        points: [
            { points: 3, hits: 2, trunk: 1, rotation_trunk: 0, head: 0, rotation_head: 0, punch: 0, penalties: 0 },
            { points: 9, hits: 4, trunk: 1, rotation_trunk: 0, head: 1, rotation_head: 0, punch: 1, penalties: 1 },
            { points: 16, hits: 8, trunk: 3, rotation_trunk: 0, head: 2, rotation_head: 1, punch: 1, penalties: 2 },
        ],
    },
};

export const nextRoundFixture = {
    match_id: 104,
    blue: { name: 'Kim Minjun' },
    red: { name: 'Alvarez Diego' },
};

export const tickerMessages = [
    'BREAKING NEWS: MATCH DELAYED DUE TO TECHNICAL DIFFICULTIES...',
    'FINAL CALL FOR REGISTRATION AT THE COMPETITOR DESK',
    'NEXT UP: SEMIFINAL BOUTS ON MAT 2',
];