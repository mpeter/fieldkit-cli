### Clean up abandoned pytest session directories safely

Pytest now marks its temporary config directories with a held ownership lock. Later sessions remove only unlocked marked directories and warn if cleanup fails, while leaving live and legacy directories untouched.
