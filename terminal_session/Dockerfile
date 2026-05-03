FROM node:18

# App directory
WORKDIR /usr/src/app

# Update package list and install sshpass
RUN apt-get update && apt-get install -y sshpass && rm -rf /var/lib/apt/lists/*

# Install app dependencies
# A wildcard is used to ensure both package.json AND package-lock.json are copied
# where available (npm@5+)
COPY package*.json ./

RUN npm install
# If you are building your code for production
# RUN npm ci --only=production

# Bundle app source
COPY . .

EXPOSE 3000

RUN mkdir /bottom

CMD [ "node", "app.js", "/bottom"]

