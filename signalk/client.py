#!/usr/bin/env python
#
#   Copyright (C) 2016 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

import kjson, socket, select, sys, os, time
from values import *
import server

try:
    import serial
except:
    pass

class ConnectionLost(Exception):
    pass

#POLLRDHUP = 0x2000

class SignalKClient(object):
    def __init__(self, f_on_connected, host=False, port=False, autoreconnect=False, have_watches=False):
        self.autoreconnect = autoreconnect

        config = {}
        configfilepath = os.getenv('HOME') + '/.pypilot/'
        if not os.path.exists(configfilepath):
            os.makedirs(configfilepath)
        if not os.path.isdir(configfilepath):
            raise configfilepath + 'should be a directory'
        configfilename = configfilepath + 'signalk.conf'

        try:
            file = open(configfilename)
            config = kjson.loads(file.readline())

            if 'host' in config and not host:
                host = config['host']
        except:
            print 'failed to read config file:', configfilename

        if host:
            try:
                config['host'] = host
                file = open(configfilename, 'w')
                file.write(kjson.dumps(config) + '\n')
            except IOError:
                print 'failed to write config file:', configfilename

        if not host:
            host = 'pypilot'
            print 'host not specified using host', host

        if '/dev' in host: # serial port
            device, baud = host, port
            if not baud or baud == 21311:
                baud = 9600
            connection = serial.Serial(device, baud)
            cmd = 'stty -F ' + device + ' icanon iexten'
            print 'running', cmd
            os.system(cmd)
        else:
            if not port:
                if ':' in host:
                    i = host.index(':')
                    host = host[:i]
                    port = host[i+1:]
                else:
                    port = server.DEFAULT_PORT
            try:
                connection = socket.create_connection((host, port), 1)
            except:
                print 'connect failed to %s:%d' % (host, port)
                raise

            self.host_port = host, port
        self.f_on_connected = f_on_connected
        self.have_watches = have_watches
        self.onconnected(connection)

    def onconnected(self, connection):
        self.socket = server.LineBufferedNonBlockingSocket(connection)
        self.values = []
        self.msg_queue = []
        self.f_on_connected(self)
        self.poller = select.poll()
        if self.socket:
            fd = self.socket.socket.fileno()
        else:
            fd = self.serial.fileno()
        self.poller.register(fd, select.POLLIN)


    def poll(self, timeout = 0):
        t0 = time.time()
        self.socket.flush()
        events = self.poller.poll(1000.0 * timeout)
        if events != []:
            event = events.pop()
            fd, flag = event
            if flag & (select.POLLERR | select.POLLNVAL):
                raise ConnectionLost
            if flag & select.POLLIN:
                if self.socket and not self.socket.recv():
                    raise ConnectionLost
                return True
        return False

    def send(self, request):
        self.socket.send(kjson.dumps(request)+'\n')

    def receive_line(self, timeout = 0):
        line = self.socket.readline()
        if line:
            try:
                msg = kjson.loads(line.rstrip())
            except:
                raise Exception('invalid message from server:', line)
            return msg

        if timeout < 0:
            return False

        t = time.time()
        try:
            if not self.poll(timeout):
                return False
        except ConnectionLost:
            self.disconnected()
        dt = time.time()-t
        return self.receive_line(timeout - dt)

    def disconnected(self):
        self.socket.socket.close()
        if not self.autoreconnect:
            raise ConnectionLost
                
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        while True:
            print 'Disconnected.  Reconnecting in 3...'
            time.sleep(3)
            try:
                connection.connect(self.host_port)
                print 'Connected.'
                break
            except:
                continue

        self.onconnected(connection)

    def receive(self, timeout = 0):
        ret = {}
        msg = self.receive_single(timeout)
        while msg:
            name, value = msg
            ret[name] = value
            msg = self.receive_single(-1)
        return ret

    def receive_single(self, timeout = 0):
        if len(self.msg_queue) > 0:
            msg = self.msg_queue[0]
            self.msg_queue = self.msg_queue[1:]
            return msg
        
        line = self.receive_line(timeout)
        if line:
            self.msg_queue += self.flatten_line(line)
            return self.receive_single(-1)
        return False

    def flatten_line(self, line, name_prefix=''):
        msgs = []
        for name in line:
            msg = line[name]
            if type(msg) == type({}):
                if 'value' in msg or 'type' in msg:
                    msgs.append((name_prefix + name, msg))
                else:
                    msgs += self.flatten_line(msg, name_prefix + name + '/')
        return msgs

    def list_values(self, timeout=10):
        request = {'method' : 'list'}
        self.send(request)
        return self.receive(timeout)

    def get(self, name):
        request = {'method' : 'get', 'name' : name}
        self.send(request)

    def set(self, name, value):
        # quote strings
        if type(value) == type(''):
            value = '"' + value + '"'
        elif type(value) == type(True):
            value = 'true' if value else 'false'
                                        
        request = '{"method": "set", "name": "' + name + '", "value": ' + str(value) + '}\n'
        self.socket.send(request)

    def watch(self, name, value=True):
        self.get(name)
        request = {'method' : 'watch', 'name' : name, 'value' : value}
        self.send(request)

    def print_values(self, timeout, info=False):
        t0 = time.time()
        if not self.values:
            self.values = self.list_values(timeout)
            if not self.values:
                return False

        names = sorted(self.values)
            
        count = 0
        results = {}
        while count < len(self.values):
            if names:
                self.get(names.pop())
            else:
                time.sleep(.05)

            if time.time()-t0 >= timeout:
                return False
            msgs = self.receive()
            for name in msgs:
                count+=1
                results[name] = msgs[name]

        for name in sorted(results):
            if info:
                print name, self.values[name], results[name]
            else:
                maxlen = 80
                result = str(results[name]['value'])
                if len(name) + len(result) + 3  > maxlen:
                    result = result[:80 - len(name) - 7] + ' ...'
                print name, '=', result
        return True
            

def SignalKClientFromArgs(argv, watch, *cargs):
    host = False
    port = False
    watches = argv[1:]
    if len(argv) > 1:
        if ':' in argv[1]:
            i = argv[1].index(':')
            host = argv[1][:i]
            port = int(argv[1][i+1:])
            watches = watches[1:]
        elif not '/' in argv[1]:
            host = argv[1]
            watches = watches[1:]

    def on_con(client):
        for arg in watches:
            if watch:
                client.watch(arg)
            else:
                client.get(arg)
        if len(cargs) == 1:
            cargs[0](client)
            
    return SignalKClient(on_con, host, port, autoreconnect=True, have_watches=watches)

# this simple test client for an autopilot server
# connects, enumerates the values, and then requests
# each value, printing them
def main():
    if '-h' in sys.argv:
        print 'usage', sys.argv[0], '[host] -i -c -h [NAME]...'
        print '-i', 'print info about each value type'
        print '-c', 'continuous watch'
        print '-h', 'show this message'
        exit(0)

    continuous = '-c' in sys.argv
    if continuous:
        sys.argv.remove('-c')

    info = '-i' in sys.argv
    if info:
        sys.argv.remove('-i')
        
    client = SignalKClientFromArgs(sys.argv, continuous)
    if not client.have_watches:
        while True:
            if not client.print_values(10, info):
                print 'timed out'
                exit(1)
            if not continuous:
                break
        exit()

    while True:
        result = client.receive(1000)
        if result:
            print kjson.dumps(result)
            if not continuous:
                return

if __name__ == '__main__':
    main()
