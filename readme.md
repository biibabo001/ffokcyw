python main.py screen --threads 4       # 全量扫描
python main.py screen --limit 100        # 冒烟测试
python main.py diagnose 中国核电          # 诊股不变

测试100只：
python wyckoff_screen.py --limit 100 --threads 5
python wyckoff_screen.py --limit 100 --charts 5
python wyckoff_screen.py --limit 500 --charts 10
python wyckoff_screen.py --charts 20
腾讯市值接口出现问题，可以先跳过市值过滤：
python wyckoff_screen.py --limit 100 --no-cap-filter