"""Seed demo handymen so the public directory is never empty. Safe to re-run.

The profile text, ratings and delivered-job ledgers below are fixed demo data;
nothing here is randomly generated.
"""

import app as m
# Six credible demo profiles across three states, so every filter dropdown
# (trade / state / pay range) still returns at least one result.
DEMO = [
    {'name': 'Chidi Okafor', 'trade': 'Carpenter', 'second': 'Roofer', 'pay': 'medium',
     'area': 'Lekki, Ajah', 'state': 'Lagos', 'city': 'Lekki', 'years': 8, 'jobs': 20,
     'bio': 'Carpenter and roofer - roof trusses, doors, wardrobes and general woodwork.',
     'rating': 5, 'reviews': 3},
    {'name': 'Amaka Eze', 'trade': 'Electrician', 'second': 'Generator Technician', 'pay': 'high',
     'area': 'Ikoyi, Victoria Island', 'state': 'Lagos', 'city': 'Ikoyi', 'years': 12, 'jobs': 14,
     'bio': 'Certified electrician for house wiring, inverters, solar and industrial fittings.',
     'rating': 5, 'reviews': 2},
    {'name': 'Bola Adeyemi', 'trade': 'Plumber', 'second': '', 'pay': 'low',
     'area': 'Surulere, Yaba', 'state': 'Lagos', 'city': 'Surulere', 'years': 4, 'jobs': 6,
     'bio': 'Affordable plumbing repairs, blocked drains, leaking pipes and pump fittings.',
     'rating': 4, 'reviews': 1, 'available': 0},
    {'name': 'Tunde Bakare', 'trade': 'Painter', 'second': 'Interior Decorator', 'pay': 'medium',
     'area': 'Gwarinpa, Life Camp', 'state': 'FCT', 'city': 'Abuja', 'years': 6, 'jobs': 9,
     'bio': 'Interior and exterior painting with premium finishes, screeding and POP.',
     'rating': 5, 'reviews': 1},
    {'name': 'Grace Nwosu', 'trade': 'Tiler', 'second': 'Mason', 'pay': 'medium',
     'area': 'Wuse, Maitama', 'state': 'FCT', 'city': 'Abuja', 'years': 7, 'jobs': 8,
     'bio': 'Tiling and masonry - floors, bathrooms, kitchen splashbacks and screeding.',
     'rating': 4, 'reviews': 2},
    {'name': 'Ibrahim Musa', 'trade': 'HVAC Technician', 'second': 'Generator Technician', 'pay': 'high',
     'area': 'Port Harcourt, GRA', 'state': 'Rivers', 'city': 'Port Harcourt', 'years': 10, 'jobs': 12,
     'bio': 'Air-conditioning installation, servicing and cold-room maintenance.',
     'rating': 4, 'reviews': 2},
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

        created += 1
    c.execute('''INSERT INTO handyman_profiles(user_id,full_name,trade,secondary_trades,bio,
        years_experience,pay_range,service_area,state,city,available,rating_sum,rating_count)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name=excluded.full_name, trade=excluded.trade,
            secondary_trades=excluded.secondary_trades, bio=excluded.bio,
            years_experience=excluded.years_experience, pay_range=excluded.pay_range,
            service_area=excluded.service_area, state=excluded.state, city=excluded.city,
            available=excluded.available, rating_sum=excluded.rating_sum,
            rating_count=excluded.rating_count, updated_at=CURRENT_TIMESTAMP''',
        (uid, d['name'], d['trade'], d['second'], d['bio'], d['years'], d['pay'], d['area'],
         d['state'], d['city'], int(d.get('available', 1)), d['rating'] * d['reviews'], d['reviews']))
rows = c.execute('''SELECT full_name,trade,state,links,jobs_completed,available
    FROM handyman_profiles ORDER BY links DESC''').fetchall()
c.close()
print('created users:', created)
print('profiles:')
for r in rows:
    print('  -', r['full_name'], '|', r['trade'], '|', r['state'], '|', r['jobs_completed'],
          'jobs |', r['links'], 'links |', 'available' if r['available'] else 'unavailable')
print('directory size:', len(m.handyman_directory()))
