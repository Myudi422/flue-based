#!/bin/bash

# Update system
apt update && apt upgrade -y

# Install dependencies
apt install -y nginx mysql-server php7.4 php7.4-fpm php7.4-mysql unzip wget certbot python3-certbot-nginx phpmyadmin

# Enable and start services
systemctl enable nginx mysql php7.4-fpm
systemctl start nginx mysql php7.4-fpm

# Configure PHP upload limits
sed -i 's/upload_max_filesize = .*/upload_max_filesize = 100M/' /etc/php/7.4/fpm/php.ini
sed -i 's/post_max_size = .*/post_max_size = 100M/' /etc/php/7.4/fpm/php.ini

# Restart PHP-FPM
systemctl restart php7.4-fpm

# Download website files
mkdir -p /var/www/ccgnimex.my.id
cd /var/www
wget -O flue.zip "https://143.198.85.46:33413/down/3029veVuGVx0.zip"
unzip flue.zip -d /var/www
rm flue.zip
chown -R www-data:www-data /var/www/ccgnimex.my.id
chmod -R 755 /var/www/ccgnimex.my.id

# Configure MySQL
echo "CREATE DATABASE IF NOT EXISTS ccgnimex;" | mysql -u root
echo "CREATE USER IF NOT EXISTS 'ccgnimex'@'localhost' IDENTIFIED BY 'aaaaaaac';" | mysql -u root
echo "GRANT ALL PRIVILEGES ON ccgnimex.* TO 'ccgnimex'@'localhost';" | mysql -u root
echo "FLUSH PRIVILEGES;" | mysql -u root

# Download and import database
cd /tmp
wget -O ccgnimex.sql.gz "https://143.198.85.46:33413/down/zBGMhyjcAb3p.gz"
gunzip ccgnimex.sql.gz
mysql -u ccgnimex -p'aaaaaaac' ccgnimex < ccgnimex.sql
rm ccgnimex.sql

# Configure Nginx
cat > /etc/nginx/sites-available/ccgnimex.my.id <<EOL
server {
    listen 80;
    server_name ccgnimex.my.id;
    root /var/www/ccgnimex.my.id;
    index index.php index.html;

    client_max_body_size 100M;

    location / {
        try_files \$uri \$uri/ /index.php?\$args;
    }

    location ~ \\.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php7.4-fpm.sock;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        include fastcgi_params;
    }

    location ~ /\.ht {
        deny all;
    }

    location /phpmyadmin {
        root /usr/share/;
        index index.php index.html index.htm;
        location ~ ^/phpmyadmin/(.+\.php)\$ {
            try_files \$uri =404;
            root /usr/share/;
            fastcgi_pass unix:/run/php/php7.4-fpm.sock;
            fastcgi_index index.php;
            fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
            include fastcgi_params;
        }
        location ~* ^/phpmyadmin/(.+\.(jpg|jpeg|gif|css|png|js|ico|html|xml|txt))$ {
            root /usr/share/;
        }
    }
}
EOL

ln -s /etc/nginx/sites-available/ccgnimex.my.id /etc/nginx/sites-enabled/
systemctl restart nginx

# Install SSL
certbot --nginx -d ccgnimex.my.id --non-interactive --agree-tos -m admin@ccgnimex.my.id

# Allow ports 80 & 443
ufw allow 80/tcp
ufw allow 443/tcp
ufw reload

# Final message
echo "Setup complete! Visit https://ccgnimex.my.id"
echo "phpMyAdmin available at: https://ccgnimex.my.id/phpmyadmin"
