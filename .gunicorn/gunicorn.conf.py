import multiprocessing

# Dirección y puerto de escucha
bind = "0.0.0.0:5000"

# Hilos de ejecución (Workers) basados en núcleos de CPU
workers = multiprocessing.cpu_count() * 2 + 1

# Tiempo de espera para peticiones (segundos)
timeout = 30

# Mantener conexión activa
keepalive = 2

# Nivel de log (info, debug, warning, error, critical)
loglevel = "info"
accesslog = "-"
errorlog = "-"
