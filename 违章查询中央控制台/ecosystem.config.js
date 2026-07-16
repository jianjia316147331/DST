module.exports = {
  apps: [
    {
      name: 'console-server',
      cwd: './cloud-server',
      script: 'npx',
      args: 'tsx watch src/index.ts',
      interpreter: 'none',
      env: {
        NODE_ENV: 'production',
      },
      // Restart if memory exceeds 512MB
      max_memory_restart: '512M',
      // Restart delay on crash
      min_uptime: '10s',
      max_restarts: 10,
      restart_delay: 3000,
      // Log config
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      error_file: '/tmp/console-server-error.log',
      out_file: '/tmp/console-server-out.log',
      merge_logs: true,
    },
    {
      name: 'console-frontend',
      cwd: './cloud-frontend',
      script: 'npx',
      args: 'vite --host 0.0.0.0',
      interpreter: 'none',
      env: {
        NODE_ENV: 'production',
      },
      max_memory_restart: '512M',
      min_uptime: '10s',
      max_restarts: 10,
      restart_delay: 3000,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      error_file: '/tmp/console-frontend-error.log',
      out_file: '/tmp/console-frontend-out.log',
      merge_logs: true,
    },
  ],
};
