"""Seed a few demo handymen for manual/QA browsing. Safe to re-run."""

import app as m

DEMO = [
    {'name': 'Chidi Okafor', 'trade': 'Carpenter', 'second': 'Roofer', 'pay': 'medium',
     'area': 'Lekki, Ajah', 'state': 'Lagos', 'city': 'Lekki', 'years': 8, 'jobs': 20,
     'bio': 'Carpenter specialising in roofs, doors, wardrobes and general woodwork.',
     'rating': 5, 'reviews': 3},
    {'name': 'Amaka Eze', 'trade': 'Electrician', 'second': 'Generator Technician', 'pay': 'high',
     'area': 'Ikoyi, Victoria Island', 'state': 'Lagos', 'city': 'Ikoyi', 'years': 12, 'jobs': 14,
     'bio': 'Certified electrician for wiring, inverters and industrial fittings.',
     'rating': 5, 'reviews': 2},
    {'name': 'Bola Adeyemi', 'trade': 'Plumber', 'second': '', 'pay': 'low',
     'area': 'Surulere, Yaba', 'state': 'Lagos', 'city': 'Surulere', 'years': 4, 'jobs': 6,
     'bio': 'Affordable plumbing repairs, blocked drains and leaking pipes.',
     'rating': 4, 'reviews': 1},
    {'name': 'Tunde Bakare', 'trade': 'Painter', 'second': 'Interior Decorator', 'pay': 'medium',
     'area': 'Gwarinpa', 'state': 'FCT', 'city': 'Abuja', 'years': 6, 'jobs': 9,
     'bio': 'Interior and exterior painting with premium finishes.',
     'rating': 5, 'reviews': 1},
]

c = m.db()
created = 0
for d in DEMO:
    email = d['name'].split()[0].lower() + '.demo@handymen.local'
    row = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    if row:
        uid = row['id']
    else:
        cur = c.execute('INSERT INTO users(email,username,password,role,phone) VALUES(?,?,?,?,?)',
                        (email, d['name'], m.generate_password_hash('demo12345'), 'handyman', '08000000'))
        uid = cur.lastrowid
        created += 1
    c.execute('''INSERT OR IGNORE INTO handyman_profiles(user_id,full_name,trade,secondary_trades,bio,
        years_experience,pay_range,service_area,state,city,available,rating_sum,rating_count)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (uid, d['name'], d['trade'], d['second'], d['bio'], d['years'], d['pay'], d['area'],
         d['state'], d['city'], 1, d['rating'] * d['reviews'], d['reviews']))
    # Give each demo handyman a realistic delivered-job ledger so links/star vary.
    have = c.execute('SELECT COUNT(*) FROM handyman_jobs WHERE handyman_user_id=?', (uid,)).fetchone()[0]
    for n in range(max(0, d['jobs'] - have)):
        c.execute('INSERT INTO handyman_jobs(handyman_user_id,title,trade) VALUES(?,?,?)',
                  (uid, f'Delivered job #{n + 1}', d['trade']))
    m.sync_handyman_profile(c, uid)
c.commit()

rows = c.execute('''SELECT full_name,trade,links,jobs_completed FROM handyman_profiles ORDER BY links DESC''').fetchall()
c.close()
print('created users:', created)
print('links:', [dict(r) for r in rows])
print('directory size:', len(m.handyman_directory()))
